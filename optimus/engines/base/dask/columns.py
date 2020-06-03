import builtins
import re
import unicodedata

import dask
import dask.dataframe as dd
import fastnumbers
import numpy as np
import pandas as pd
from dask import delayed
from dask.dataframe import from_delayed
from dask.dataframe.core import DataFrame
from dask_ml.impute import SimpleImputer
from multipledispatch import dispatch
from numba import jit
from sklearn.preprocessing import MinMaxScaler

from optimus.engines.base.columns import BaseColumns
from optimus.engines.dask.ml.encoding import index_to_string as ml_index_to_string
from optimus.engines.dask.ml.encoding import string_to_index as ml_string_to_index
from optimus.helpers.check import equal_function, is_cudf_series, is_pandas_series
from optimus.helpers.columns import parse_columns, validate_columns_names, check_column_numbers, get_output_cols
from optimus.helpers.constants import RELATIVE_ERROR, Actions, PROFILER_NUMERIC_DTYPES, \
    PROFILER_STRING_DTYPES
from optimus.helpers.converter import format_dict
from optimus.helpers.core import val_to_list, one_list_to_val
from optimus.helpers.functions import update_dict
from optimus.helpers.raiseit import RaiseIt
from optimus.infer import Infer, is_list, is_list_of_tuples, is_one_element, is_int, profiler_dtype_func, is_dict, \
    is_str
from optimus.infer import is_
from optimus.profiler.functions import fill_missing_var_types

MAX_BUCKETS = 33


# This implementation works for Dask and dask_cudf
@jit
def _min(value):
    return np.min(value)


TOTAL_PREVIEW_ROWS = 30


class DaskBaseColumns(BaseColumns):

    def __init__(self, df):
        super(DaskBaseColumns, self).__init__(df)

    def count_mismatch(self, columns_mismatch: dict = None, infer=True, compute=True):
        df = self.df
        if not is_dict(columns_mismatch):
            columns_mismatch = parse_columns(df, columns_mismatch)
        init = {0: 0, 1: 0, 2: 0}

        @delayed
        def count_dtypes(_df, _col_name, _func_dtype):

            def _func(value):

                # match data type
                if _func_dtype(value):
                    # ProfilerDataTypesQuality.MATCH.value
                    return 2

                elif pd.isnull(value):
                    # ProfilerDataTypesQuality.MISSING.value
                    return 1

                # mismatch
                else:
                    # ProfilerDataTypesQuality.MISMATCH.value
                    return 0

            r = _df[_col_name].map(_func).value_counts().to_dict()
            r = update_dict(init.copy(), r)
            a = {_col_name: {"mismatch": r[0], "missing": r[1], "match": r[2]}}
            return a

        partitions = df.to_delayed()

        delayed_parts = [count_dtypes(part, col_name, profiler_dtype_func(dtype, True)) for part in
                         partitions for col_name, dtype in columns_mismatch.items()]

        @delayed
        def merge(_pdf):
            columns = set(list(i.keys())[0] for i in _pdf)
            r = {col_name: {"mismatch": 0, "missing": 0, "match": 0} for col_name in columns}

            for l in _pdf:
                for i, j in l.items():
                    r[i]["mismatch"] = r[i]["mismatch"] + j["mismatch"]
                    r[i]["missing"] = r[i]["missing"] + j["missing"]
                    r[i]["match"] = r[i]["match"] + j["match"]

            return r

        # TODO: Maybe we can use a reduction here https://docs.dask.org/en/latest/dataframe-api.html#dask.dataframe.Series.reduction
        b = merge(delayed_parts)

        if compute is True:
            result = dd.compute(b)[0]
        else:
            result = b
        return result

    def h_freq(self, columns):
        from dask import dataframe as dd
        from dask import delayed

        col_name = "OFFENSE_CODE_GROUP"
        # columns =["OFFENSE_CODE_GROUP"]
        result = {}

        @delayed
        def func(df):
            df["hash"] = df[col_name].apply(hash)
            return df

        partitions = df.to_delayed()

        for col_name in columns:
            delayed_parts = [func(part) for part in partitions]
        #     m = df["hash"].value_counts().nlargest().to_dict()

        #     # print(m)
        #     for l, n in m.items():
        #         print(df[df["hash"] == l].iloc[0][col_name], n)
        dd.from_delayed(delayed_parts).compute()

    def count_uniques(self, columns, estimate: bool = True, compute: bool = True):
        df = self.df
        columns = parse_columns(df, columns)

        @delayed
        def _count_uniques(_df):
            return _df.nunique()

        @delayed
        def merge(lu, columns):
            return {column: {"count_uniques": u} for column, u in zip(columns, lu)}

        count_uniques_values = [_count_uniques(df[col_name]) for col_name in columns]
        result = merge(count_uniques_values, columns)

        if compute is True:
            result = result.compute()

        return result

    def frequency(self, columns, n=MAX_BUCKETS, percentage=False, total_rows=None, count_uniques=False, compute=True):

        df = self.df
        columns = parse_columns(df, columns)

        @delayed
        def series_to_dict(_series, _total_freq_count=None):

            if is_pandas_series(_series):
                result = [{"value": i, "count": j} for i, j in _series.to_dict().items()]

            elif is_cudf_series(_series):
                r = {i[0]: i[1] for i in _series.to_frame().to_records()}
                result = [{"value": i, "count": j} for i, j in r.items()]

            if _total_freq_count is None:
                result = {_series.name: {"frequency": result}}
            else:
                result = {_series.name: {"frequency": result, "count_uniques": int(_total_freq_count)}}

            return result

        @delayed
        def flat_dict(top_n):

            result = {key: value for ele in top_n for key, value in ele.items()}
            return result

        @delayed
        def freq_percentage(_value_counts, _total_rows):

            for i, j in _value_counts.items():
                for x in list(j.values())[0]:
                    x["percentage"] = round((x["count"] * 100 / _total_rows), 2)

            return _value_counts

        value_counts = [df[col_name].value_counts().to_delayed()[0] for col_name in columns]

        n_largest = [_value_counts.nlargest(n) for _value_counts in value_counts]

        if count_uniques is True:
            count_uniques = [_value_counts.count() for _value_counts in value_counts]
            b = [series_to_dict(_n_largest, _count) for _n_largest, _count in zip(n_largest, count_uniques)]
        else:
            b = [series_to_dict(_n_largest) for _n_largest in n_largest]

        c = flat_dict(b)

        if percentage:
            c = freq_percentage(c, delayed(len)(df))

        if compute is True:
            result = c.compute()
        else:
            result = c

        return result

    def hist(self, columns, buckets=20, compute=True):

        df = self.df
        columns = parse_columns(df, columns)

        @delayed
        def bins_col(_columns, _min, _max):

            return {col_name: list(np.linspace(_min[col_name], _max[col_name], num=buckets)) for col_name in _columns}

        _min = df[columns].min().to_delayed()[0]
        _max = df[columns].max().to_delayed()[0]
        _bins = bins_col(columns, _min, _max)

        @delayed
        def _hist(pdf, col_name, _bins):
            p = [fastnumbers.fast_real(x, default=np.nan) for x in pdf[col_name]]
            _count, bins_edges = np.histogram(p, bins=_bins[col_name])
            return {col_name: [list(_count), list(bins_edges)]}

        @delayed
        def _agg_hist(values):
            _result = {}
            x = np.zeros(buckets - 1)
            for i in values:
                for j in i:
                    t = i.get(j)
                    if t is not None:
                        _count = np.sum([x, t[0]], axis=0)
                        _bins = t[1]
                        col_name = j
                l = len(_count)
                r = [{"lower": float(_bins[i]), "upper": float(_bins[i + 1]),
                      "count": int(_count[i])} for i in range(l)]
                _result[col_name] = {"hist": r}

            return _result

        partitions = df.to_delayed()
        c = [_hist(part, col_name, _bins) for part in partitions for col_name in columns]

        d = _agg_hist(c)

        if compute is True:
            result = d.compute()
        else:
            result = d
        return result

    @staticmethod
    def mode(columns):
        pass

    @staticmethod
    def abs(columns):
        pass

    @staticmethod
    def bucketizer(input_cols, splits, output_cols=None):
        pass

    def index_to_string(self, input_cols=None, output_cols=None, columns=None):
        df = self.df

        df = ml_index_to_string(df, input_cols, output_cols, columns)

        return df

    def string_to_index(self, input_cols=None, output_cols=None, columns=None):

        """
        Encodes a string column of labels to a column of label indices
        :param input_cols:
        :param output_cols:
        :param columns:
        :return:
        """
        df = self.df

        df = ml_string_to_index(df, input_cols, output_cols, columns)

        return df

    @staticmethod
    def values_to_cols(input_cols):
        pass

    def clip(self, columns, lower_bound, upper_bound):
        df = self.df
        columns = parse_columns(df, columns)
        df[columns] = df[columns].clip(lower_bound, upper_bound)

        return df

    def qcut(self, columns, num_buckets, handle_invalid="skip"):
        #
        # df = self.df
        # columns = parse_columns(df, columns)
        # df[columns] = df[columns].map_partitions(pd.cut, num_buckets)
        # return df
        pass

    @staticmethod
    def boxplot(columns):
        pass

    @staticmethod
    def correlation(input_cols, method="pearson", output="json"):
        pass

    def count(self):
        df = self.df
        return len(df.columns)

    @staticmethod
    def frequency_by_group(columns, n=10, percentage=False, total_rows=None):
        pass

    @staticmethod
    def scatter(columns, buckets=10):
        pass

    @staticmethod
    def cell(column):
        pass

    def iqr(self, columns, more=None, relative_error=RELATIVE_ERROR):
        """
        Return the column Inter Quartile Range
        :param columns:
        :param more: Return info about q1 and q3
        :param relative_error:
        :return:
        """
        df = self.df
        iqr_result = {}
        columns = parse_columns(df, columns, filter_by_column_dtypes=df.constants.NUMERIC_TYPES)

        check_column_numbers(columns, "*")

        quartile = df.cols.percentile(columns, [0.25, 0.5, 0.75], relative_error=relative_error)
        # print(quartile)
        for col_name in columns:

            q1 = quartile[col_name]["percentile"]["0.25"]
            q2 = quartile[col_name]["percentile"]["0.5"]
            q3 = quartile[col_name]["percentile"]["0.75"]

            iqr_value = q3 - q1
            if more:
                result = {"iqr": iqr_value, "q1": q1, "q2": q2, "q3": q3}
            else:
                result = iqr_value
            iqr_result[col_name] = result
        return format_dict(iqr_result)

    @staticmethod
    def standard_scaler():
        pass

    @staticmethod
    def max_abs_scaler(input_cols, output_cols=None):
        pass

    def min_max_scaler(self, input_cols, output_cols=None):
        # https://github.com/dask/dask/issues/2690

        df = self.df

        scaler = MinMaxScaler()

        input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)

        # _df = df[input_cols]
        scaler.fit(df[input_cols])
        # print(type(scaler.transform(_df)))
        arr = scaler.transform(df[input_cols])
        darr = dd.from_array(arr)
        # print(type(darr))
        darr.name = 'z'
        df = df.merge(darr)

        return df

    def z_score(self, input_cols, output_cols=None):
        df = self.df
        input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)

        for input_col, output_col in zip(input_cols, output_cols):
            df[output_col] = (df[input_col] - df[input_col].mean()) / df[input_col].std(ddof=0)
        return df

    @staticmethod
    def _math(columns, operator, new_column):
        pass

    @staticmethod
    def select_by_dtypes(data_type):
        pass

    @staticmethod
    def nunique(*args, **kwargs):
        pass

    def unique(self, columns):
        df = self.df
        return df.drop_duplicates()

    def value_counts(self, columns):
        """
        Return the counts of uniques values
        :param columns:
        :return:
        """
        df = self.df
        columns = parse_columns(df, columns)
        # .value(columns, 1)

        result = {}
        for col_name in columns:
            result.update(df[col_name].value_counts().compute().to_frame().to_dict())
        return result

    def extract(self, input_cols, output_cols, regex):
        df = self.df
        from optimus.engines.base.dataframe.commons import extract
        df = extract(df, input_cols, output_cols, regex)
        return df

    def slice(self, input_cols, output_cols, start, stop, step):
        df = self.df

        input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)

        for input_col, output_col in zip(input_cols, output_cols):
            df[output_col] = df[input_col].str.slice(start, stop, step)
        return df

    def count_na(self, columns):
        """
        Return the NAN and Null count in a Column
        :param columns: '*', list of columns names or a single column name.
        :return:
        """
        df = self.df
        return self.agg_exprs(columns, df.functions.count_na_agg, self)

    def count_zeros(self, columns):
        """
        Count zeros in a column
        :param columns: '*', list of columns names or a single column name.
        :return:
        """
        df = self.df
        return self.agg_exprs(columns, df.functions.zeros_agg)

    # def count_uniques(self, columns, estimate=True, compute=True):
    #     """
    #     Return how many unique items exist in a columns
    #     :param columns: '*', list of columns names or a single column name.
    #     :param estimate: If true use HyperLogLog to estimate distinct count. If False use full distinct
    #     :type estimate: bool
    #     :return:
    #     """
    #     print("count_uniques")
    #     df = self.df
    #     return self.agg_exprs(columns, df.functions.count_uniques_agg, estimate)

    def is_na(self, input_cols, output_cols=None):
        """
        Replace null values with True and non null with False
        :param input_cols: '*', list of columns names or a single column name.
        :param output_cols:
        :return:
        """

        def _is_na(value):
            return value is np.NaN

        df = self.df

        return df.cols.apply(input_cols, _is_na, output_cols=output_cols)

    def impute(self, input_cols, data_type="continuous", strategy="mean", output_cols=None):
        """

        :param input_cols:
        :param data_type:
        :param strategy:
        # - If "mean", then replace missing values using the mean along
        #   each column. Can only be used with numeric data.
        # - If "median", then replace missing values using the median along
        #   each column. Can only be used with numeric data.
        # - If "most_frequent", then replace missing using the most frequent
        #   value along each column. Can be used with strings or numeric data.
        # - If "constant", then replace missing values with fill_value. Can be
        #   used with strings or numeric data.
        :param output_cols:
        :return:
        """

        df = self.df
        imputer = SimpleImputer(strategy=strategy, copy=False)

        input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)

        _df = df[input_cols]
        imputer.fit(_df)
        # df[output_cols] = imputer.transform(_df)[input_cols]
        df[output_cols] = imputer.transform(_df)[input_cols]
        return df

    # Date operations
    @staticmethod
    def years_between(input_cols, date_format=None, output_cols=None):
        pass

    @staticmethod
    def to_timestamp(input_cols, date_format=None, output_cols=None):
        pass

    @staticmethod
    def date_transform(input_cols, current_format=None, output_format=None, output_cols=None):
        pass

    def year(self, input_cols, output_cols=None):
        """

        :param input_cols:
        :param output_cols:
        :return:
        """
        df = self.df

        input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)
        for input_col, output_col in zip(input_cols, output_cols):
            df = df.assign(**{output_col: df[input_col].dt.year})
        return df

    def month(self, input_cols, output_cols=None):
        """

        :param input_cols:
        :param output_cols:
        :return:
        """
        df = self.df

        input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)
        for input_col, output_col in zip(input_cols, output_cols):
            df = df.assign(**{output_col: df[input_col].dt.month})
        return df

    def day(self, input_cols, output_cols=None):
        """

        :param input_cols:
        :param output_cols:
        :return:
        """
        df = self.df

        input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)
        for input_col, output_col in zip(input_cols, output_cols):
            df = df.assign(**{output_col: df[input_col].dt.day})
        return df

    def hour(self, input_cols, output_cols=None):
        pass

    def minute(self, input_cols, output_cols=None):
        pass

    def second(self, input_cols, output_cols=None):
        pass

    def weekday(self, input_cols, output_cols=None):
        pass

    def weekofyear(self, input_cols, output_cols=None):
        pass

    def replace_regex(self, input_cols, regex=None, value=None, output_cols=None):
        """
        Use a Regex to replace values
        :param input_cols: '*', list of columns names or a single column name.
        :param output_cols:
        :param regex: values to look at to be replaced
        :param value: new value to replace the old one
        :return:
        """

        df = self.df

        def _replace_regex(value, regex, replace):
            return value.replace(regex, replace)

        return df.cols.apply(input_cols, func=_replace_regex, args=[regex, value], output_cols=output_cols,
                             filter_col_by_dtypes=df.constants.STRING_TYPES + df.constants.NUMERIC_TYPES)

    def remove_white_spaces(self, input_cols, output_cols=None):

        def _remove_white_spaces(value, args):
            return value.str.replace(" ", "")

        df = self.df
        return df.cols.apply(input_cols, _remove_white_spaces, func_return_type=str,
                             filter_col_by_dtypes=df.constants.STRING_TYPES,
                             output_cols=output_cols)

    def remove_special_chars(self, input_cols, output_cols=None):
        def _remove_special_chars(value, args):
            return value.str.replace('[^A-Za-z0-9]+', '')
            # return re.sub('[^A-Za-z0-9]+', '', value)

        df = self.df
        return df.cols.apply(input_cols, _remove_special_chars, func_return_type=str,
                             filter_col_by_dtypes=df.constants.STRING_TYPES,
                             output_cols=output_cols)

    def remove_accents(self, input_cols, output_cols=None):
        def _remove_accents(value, args):
            def func(_value):
                # first, normalize strings:
                nfkd_str = unicodedata.normalize('NFKD', str(_value))

                # Keep chars that has no other char combined (i.e. accents chars)
                with_out_accents = u"".join([c for c in nfkd_str if not unicodedata.combining(c)])
                return with_out_accents

            return value.map(func)

        df = self.df
        return df.cols.apply(input_cols, _remove_accents, func_return_type=str,
                             filter_col_by_dtypes=df.constants.STRING_TYPES,
                             output_cols=output_cols)

    def remove(self, input_cols, search=None, search_by="chars", output_cols=None):
        return self.replace(input_cols=input_cols, search=search, replace_by="", search_by=search_by,
                            output_cols=output_cols)

    def reverse(self, input_cols, output_cols=None):
        def _reverse(value):
            return str(value)[::-1]

        df = self.df
        return df.cols.apply(input_cols, _reverse, func_return_type=str,
                             filter_col_by_dtypes=df.constants.STRING_TYPES,
                             output_cols=output_cols)

    def drop(self, columns=None, regex=None, data_type=None):
        """
        Drop a list of columns
        :param columns: Columns to be dropped
        :param regex: Regex expression to select the columns
        :param data_type:
        :return:
        """
        df = self.df
        # if regex:
        #     r = re.compile(regex)
        #     columns = [c for c in list(df.columns) if re.match(regex, c)]
        #
        # columns = parse_columns(df, columns, filter_by_column_dtypes=data_type)
        # check_column_numbers(columns, "*")

        names = df.cols.names(columns, regex, data_type)
        df = df.drop(names, axis=1)

        df = df.meta.preserve(df, "drop", columns)

        return df

    def keep(self, columns=None, regex=None):
        """
        Drop a list of columns
        :param columns: Columns to be dropped
        :param regex: Regex expression to select the columns
        :param data_type:
        :return:
        """
        df = self.df
        if regex:
            # r = re.compile(regex)
            columns = [c for c in list(df.columns) if re.match(regex, c)]

        columns = parse_columns(df, columns)
        check_column_numbers(columns, "*")

        df = df.drop(columns=list(set(df.columns) - set(columns)))

        df = df.meta.action("keep", columns)

        return df

    @staticmethod
    def astype(*args, **kwargs):
        pass

    def set(self, where=None, value=None, output_cols=None):
        """
        Set a column value using a number a string or a expression.
        :param where:
        :param value:
        :param output_cols:
        :return:
        """
        df = self.df

        # output_cols = parse_columns(df, output_cols, accepts_missing_cols=True)

        # try to infer if we are going to handle the operations as numeric or string
        # Get first column in the operation
        def prepare_columns(cols):
            """
            Extract the columns names from the value and where clauses
            :param cols:
            :return:
            """
            if cols is not None:
                r = val_to_list([f_col[1:len(f_col) - 1] for f_col in
                                 re.findall(r"\[(['A-Za-z0-9_']+)\]", cols.replace("\"", "'"))])
            else:
                r = None
            return r

        columns = prepare_columns(value)
        if columns:
            first_columns = columns[0]
            where_columns = prepare_columns(where)
            if where_columns is not None:
                columns = columns + where_columns
            # Remove duplicated columns
            columns = list(set(columns))

            column_dtype = df.cols.profiler_dtypes(first_columns)[first_columns]
        else:

            if fastnumbers.fast_int(value):
                column_dtype = "int"
            elif fastnumbers.fast_float(value):
                column_dtype = "decimal"
            else:
                column_dtype = "string"

        if column_dtype in PROFILER_NUMERIC_DTYPES:
            vfunc = lambda x: fastnumbers.fast_float(x, default=np.nan) if x is not None else None
        elif column_dtype in PROFILER_STRING_DTYPES or column_dtype is None:
            vfunc = lambda x: str(x) if not pd.isnull(x) else None

        def func(pdf, _value, _where, _output_col):

            pdf = pdf.applymap(vfunc)

            try:
                if where is None:
                    df = pdf # <- df is used in the eval
                    return eval(_value)
                else:
                    df = pdf
                    _mask = (eval(_where))

                    mask = df[_mask]
                    df = mask
                    _value = eval(_value)  # <- mask is used here

                    df.loc[_mask, _output_col] = _value
                    return df[_output_col]
            except:
                raise
                return np.nan

        # if df.cols.dtypes(input_col) == "category":
        #     try:
        #         # Handle error if the category already exist
        #         df[input_col] = df[input_col].cat.add_categories(val_to_list(value))
        #     except ValueError:
        #         pass

        _meta = df.dtypes.to_dict()
        output_cols = one_list_to_val(output_cols)
        _meta.update({output_cols: object})
        if columns:
            final_value = df[columns].map_partitions(func, _value=value, _where=where, _output_col=output_cols,
                                                     meta=object)
        else:
            # df[output_cols] = value
            final_value = value
        df.meta.preserve(df, Actions.SET.value, output_cols)
        kw_columns = {output_cols: final_value}
        return df.assign(**kw_columns)

    @dispatch(list)
    def copy(self, columns) -> DataFrame:
        return self.copy(columns=columns)

    def copy(self, input_cols=None, output_cols=None, columns=None) -> DataFrame:
        """
        Copy one or multiple columns
        :param input_cols: Source column to be copied
        :param output_cols: Destination column
        :param columns: tuple of column [('column1','column_copy')('column1','column1_copy')()]
        :return:
        """
        df = self.df
        output_ordered_columns = df.cols.names()

        if columns is None:
            input_cols = parse_columns(df, input_cols)
            if is_list(input_cols) or is_one_element(input_cols):
                output_cols = get_output_cols(input_cols, output_cols)

        if columns:
            input_cols = list([c[0] for c in columns])
            output_cols = list([c[1] for c in columns])
            output_cols = get_output_cols(input_cols, output_cols)

        for input_col, output_col in zip(input_cols, output_cols):
            if input_col != output_col:
                col_index = output_ordered_columns.index(input_col) + 1
                output_ordered_columns[col_index:col_index] = [output_col]

        df = df.assign(**{output_col: df[input_col] for input_col, output_col in zip(input_cols, output_cols)})
        df = df.meta.copy({input_col: output_col})
        return df.cols.select(output_ordered_columns)

    @staticmethod
    def apply_by_dtypes(columns, func, func_return_type, args=None, func_type=None, data_type=None):
        pass

    @staticmethod
    def apply_expr(input_cols, func=None, args=None, filter_col_by_dtypes=None, output_cols=None, meta=None):
        pass

    @staticmethod
    def append(dfs) -> DataFrame:
        """

        :param dfs:
        :return:
        """
        # df = dd.concat([self, dfs], axis=1)
        raise NotImplementedError
        # return df

    @staticmethod
    def exec_agg(exprs):
        """
        Execute and aggregation
        :param exprs:
        :return:
        """

        # 'scheduler' param values
        # "threads": a scheduler backed by a thread pool
        # "processes": a scheduler backed by a process pool (preferred option on local machines as it uses all CPUs)
        # "single-threaded" (aka “sync”): a synchronous scheduler, good for debugging

        # import dask.multiprocessing
        # import dask.threaded
        #
        # >> > dmaster.compute(get=dask.threaded.get)  # this is default for dask.dataframe
        # >> > dmaster.compute(get=dask.multiprocessing.get)  # try processes instead
        #
        agg_results = dask.compute(*exprs)
        result = {}

        # Parsing results
        def parse_percentile(_value):
            _result = {}
            if is_(_value, pd.core.series.Series):
                _result.setdefault(_value.name, {str(i): j for i, j in dict(_value).items()})
            else:
                for (p_col_name, p_result) in _value.iteritems():
                    if is_(p_result, pd.core.series.Series):
                        p_result = dict(p_result)
                    _result.setdefault(p_col_name, {str(i): j for i, j in p_result.items()})
            return _result

        def parse_hist(_value):
            _result = {}
            for _col_name, values in _value.items():
                _hist = []

                x = values["count"]
                y = values["bins"]
                for idx, v in enumerate(y):
                    if idx < len(y) - 1:
                        _hist.append({"count": x[idx], "lower": y[idx], "upper": y[idx + 1]})
                _result.setdefault(_col_name, _hist)
            return _result

        def parse_count_uniques(_value):
            # Because count_uniques() return and Scalar and not support casting we need to make
            # the cast to int after compute()
            # Reference https://github.com/dask/dask/issues/1445
            return {_col_name: int(values) for _col_name, values in _value.items()}

        for agg_name, col_name_result in agg_results:
            if agg_name == "percentile":
                col_name_result = parse_percentile(col_name_result)
            elif agg_name == "hist":
                col_name_result = parse_hist(col_name_result)
            elif agg_name == "count_uniques":
                col_name_result = parse_count_uniques(col_name_result)

            # Process by datatype
            if is_(col_name_result, pd.core.series.Series):
                # col_name_result = pd.Series(col_name_result)
                # print("COL NAME RESULT",col_name_result)
                index = col_name_result.index
                for cols_name in index:
                    result.setdefault(cols_name, {}).update({agg_name: col_name_result[cols_name]})
            else:
                index = col_name_result
                for col_name, value in index.items():
                    result.setdefault(col_name, {}).update({agg_name: col_name_result[col_name]})

        return result

    def create_exprs(self, columns, funcs, *args):

        df = self.df
        # Std, kurtosis, mean, skewness and other agg functions can not process date columns.
        filters = {"object": [df.functions.min, df.functions.stddev],
                   }

        def _filter(_col_name, _func):
            for data_type, func_filter in filters.items():
                for f in func_filter:
                    if equal_function(func, f) and \
                            df.cols.dtypes(_col_name)[_col_name] == data_type:
                        return True
            return False

        columns = parse_columns(df, columns)
        funcs = val_to_list(funcs)

        result = {}

        for func in funcs:
            # print("FUNC", func)
            # Create expression for functions that accepts multiple columns
            filtered_column = []
            for col_name in columns:
                # If the key exist update it
                if not _filter(col_name, func):
                    filtered_column.append(col_name)
            if len(filtered_column) > 0:
                # print("ITER", col_name)
                func_result = func(columns, args)(df)
                for k, v in func_result.items():
                    result[k] = {}
                    result[k] = v
        result = list(result.items())

        return result

    # TODO: Check if we must use * to select all the columns
    @dispatch(object, object)
    def rename(self, columns_old_new=None, func=None):
        """"
        Changes the name of a column(s) dataFrame.
        :param columns_old_new: List of tuples. Each tuple has de following form: (oldColumnName, newColumnName).
        :param func: can be lower, upper or any string transformation function
        """

        df = self.df

        # Apply a transformation function
        if is_list_of_tuples(columns_old_new):
            validate_columns_names(df, columns_old_new)
            for col_name in columns_old_new:

                old_col_name = col_name[0]
                if is_int(old_col_name):
                    old_col_name = df.schema.names[old_col_name]
                if func:
                    old_col_name = func(old_col_name)

                current_meta = df.meta.get()
                # DaskColumns.set_meta(col_name, "optimus.transformations", "rename", append=True)
                # TODO: this seems to the only change in this function compare to pandas. Maybe this can be moved to a base class

                new_column = col_name[1]
                if old_col_name != col_name:
                    df = df.rename(columns={old_col_name: new_column})

                # df = df.meta.preserve(df, value=current_meta)

                df = df.meta.rename({old_col_name: new_column})

        return df

    @dispatch(list)
    def rename(self, columns_old_new=None):
        return self.rename(columns_old_new, None)

    @dispatch(object)
    def rename(self, func=None):
        return self.rename(None, func)

    @dispatch(str, str, object)
    def rename(self, old_column, new_column, func=None):
        return self.rename([(old_column, new_column)], func)

    @dispatch(str, str)
    def rename(self, old_column, new_column):
        return self.rename([(old_column, new_column)], None)

    def fill_na(self, input_cols, value=None, output_cols=None):
        """
        Replace null data with a specified value
        :param input_cols: '*', list of columns names or a single column name.
        :param output_cols:
        :param value: value to replace the nan/None values
        :return:
        """

        def _fill_na(value, new_value):
            return new_value if value is np.NaN else value

        df = self.df

        return df.cols.apply(input_cols, _fill_na, args=value, output_cols=output_cols)

    def count_by_dtypes(self, columns, infer=False, str_funcs=None, int_funcs=None, mismatch=None):
        df = self.df
        columns = parse_columns(df, columns)
        columns_dtypes = df.cols.dtypes()

        def value_counts(series):
            return series.value_counts()

        delayed_results = []

        for col_name in columns:
            a = df.map_partitions(lambda df: df[col_name].apply(
                lambda row: Infer.parse((col_name, row), infer, columns_dtypes, str_funcs, int_funcs,
                                        full=False))).compute()

            f = df.functions.map_delayed(a, value_counts)
            delayed_results.append({col_name: f.to_dict()})

        results_compute = dask.compute(*delayed_results)
        result = {}

        # Convert list to dict
        for i in results_compute:
            result.update(i)

        if infer is True:
            result = fill_missing_var_types(result, columns_dtypes)
        else:
            result = self.parse_profiler_dtypes(result)

        return result

    def lower(self, input_cols, output_cols=None):
        def _lower(value, args):
            return value.str.lower()

        df = self.df

        return df.cols.apply(input_cols, _lower, func_return_type=str,
                             filter_col_by_dtypes=df.constants.STRING_TYPES,
                             output_cols=output_cols,
                             meta_action=Actions.LOWER.value)

    def upper(self, input_cols, output_cols=None):
        def _upper(value, args):
            return value.str.upper()

        df = self.df
        return df.cols.apply(input_cols, _upper, func_return_type=str,
                             filter_col_by_dtypes=df.constants.STRING_TYPES,
                             output_cols=output_cols,
                             meta_action=Actions.UPPER.value)

    def trim(self, input_cols, output_cols=None):

        def _trim(value, args):
            return value.str.strip()

        df = self.df
        return df.cols.apply(input_cols, _trim, func_return_type=str,
                             filter_col_by_dtypes=df.constants.STRING_TYPES,
                             output_cols=output_cols,
                             meta_action=Actions.TRIM.value)

    def apply(self, input_cols, func=None, func_return_type=None, args=None, func_type=None, when=None,
              filter_col_by_dtypes=None, output_cols=None, skip_output_cols_processing=False,
              meta_action=Actions.APPLY_COLS.value):

        df = self.df

        input_cols = parse_columns(df, input_cols, filter_by_column_dtypes=filter_col_by_dtypes,
                                   accepts_missing_cols=True)

        # check_column_numbers(input_cols, "*")
        output_ordered_columns = df.cols.names()

        if skip_output_cols_processing:
            output_cols = val_to_list(output_cols)
        else:
            output_cols = get_output_cols(input_cols, output_cols)

        if output_cols is None:
            output_cols = input_cols

        # input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)

        result = {}

        partitions = df.to_delayed()

        for input_col, output_col in zip(input_cols, output_cols):
            temp = [dask.delayed(func)(part[input_col], args)
                    for part in partitions]

            result[output_col] = from_delayed(temp)

            # Preserve column order

            if output_col not in df.cols.names():
                col_index = output_ordered_columns.index(input_col) + 1
                output_ordered_columns[col_index:col_index] = [output_col]

            # Preserve actions for the profiler
            df = df.meta.preserve(df, meta_action, output_col)

        df = df.assign(**result)
        return df.cols.select(output_ordered_columns)

    # TODO: Maybe should be possible to cast and array of integer for example to array of floats
    def cast(self, input_cols=None, dtype=None, output_cols=None, columns=None, on_error=None):
        """
        Cast the elements inside a column or a list of columns to a specific data type.
        Unlike 'cast' this not change the columns data type

        :param input_cols: Columns names to be casted
        :param output_cols:
        :param dtype: final data type
        :param columns: List of tuples of column names and types to be casted. This variable should have the
                following structure:
                colsAndTypes = [('columnName1', 'integer'), ('columnName2', 'float'), ('columnName3', 'string')]
                The first parameter in each tuple is the column name, the second is the final datatype of column after
                the transformation is made.
        :return: Dask DataFrame
        """

        df = self.df
        if on_error is None:
            def _cast_int(value):
                # if (value is None) or (value is np.nan):
                if pd.isnull(value):
                    return np.nan
                else:
                    # return fastnumbers.fast_int(value, default=np.nan)
                    return fastnumbers.fast_int(value)

            def _cast_float(value):
                if pd.isnull(value):
                    return np.nan
                else:
                    return fastnumbers.fast_float(value)

            def _cast_bool(value):
                if pd.isnull(value):
                    return np.nan
                else:
                    return bool(value)

            def _cast_date(value, format="YYYY-MM-DD"):
                if pd.isnull(value):
                    return np.nan
                else:
                    try:
                        # return pendulum.parse(value)
                        # return pendulum.from_format(value, format)
                        # return dparse(value)

                        return value
                    except:
                        return value

            def _cast_str(value):
                if pd.isnull(value):
                    return np.nan
                else:
                    return str(value)

            def _cast_object(value):
                ## Do nothing
                return value

        elif on_error == "nan":
            def _cast_int(value):
                # if (value is None) or (value is np.nan):
                if pd.isnull(value):
                    return np.nan
                else:
                    # return fastnumbers.fast_int(value, default=np.nan)
                    return fastnumbers.fast_int(value, default=np.nan)

            def _cast_float(value):
                if pd.isnull(value):
                    return np.nan
                else:
                    return fastnumbers.fast_float(value, default=np.nan)

            def _cast_bool(value):
                if pd.isnull(value):
                    return np.nan
                else:
                    return bool(value)

            def _cast_date(value, format="YYYY-MM-DD"):
                if pd.isnull(value):
                    return np.nan
                else:
                    try:
                        # return pendulum.parse(value)
                        # return pendulum.from_format(value, format)
                        # return dparse(value)

                        return value
                    except:
                        return value

            def _cast_str(value):
                if pd.isnull(value):
                    return np.nan
                else:
                    return str(value)

            def _cast_object(value):
                ## Do nothing
                return value

        _dtypes = []
        # Parse params
        if columns is None:
            input_cols = parse_columns(df, input_cols)
            if is_list(input_cols) or is_one_element(input_cols):
                output_cols = get_output_cols(input_cols, output_cols)
                for _ in builtins.range(0, len(input_cols)):
                    _dtypes.append(dtype)
            # else:
            #     input_cols = list([c[0] for c in columns])
            #     if len(columns[0]) == 2:
            #         output_cols = get_output_cols(input_cols, output_cols)
            #         _dtypes = list([c[1] for c in columns])
            #     elif len(columns[0]) == 3:
            #         output_cols = list([c[1] for c in columns])
            #         _dtypes = list([c[2] for c in columns])

            output_cols = get_output_cols(input_cols, output_cols)

        cast_func = {'int': _cast_int, 'decimal': _cast_float, "string": _cast_str, 'bool': _cast_bool,
                     'date': _cast_date, "array": _cast_object, "object": _cast_object, "gender": _cast_object,
                     "ip": _cast_object, "url": _cast_object, "email": _cast_object, "credit_card_number": _cast_object,
                     "zip_code": _cast_str, "missing": _cast_str}

        def func(pdf, input_cols, output_cols, dtypes):
            # print("AAA", input_cols, output_cols, dtypes)
            for input_col, output_col, dtype in zip(input_cols, output_cols, dtypes):
                # pdf[output_col] = pdf[input_col].apply(cast_func[dtype])
                pdf = pdf.assign(**{output_col: pdf[input_col].apply(cast_func[dtype])})
            return pdf

        # print("columns",input_cols, output_cols,dtype)
        # meta = [(i, object) for i in columns]
        df = df.map_partitions(func, input_cols, output_cols, val_to_list(dtype) * len(input_cols), )
        # df.cols.set_profiler_dtypes(columns)

        ## Check this could be faster ddf = ddf.assign(col4=lambda x: check_dist(x.col1,x.col2,x.col3))
        # df = df.assign(**{output_col: df[input_col].apply(func=func, args=args, meta=meta, convert_dtype=False)})
        return df

    def nest(self, input_cols, shape="string", separator="", output_col=None):
        """
        Merge multiple columns with the format specified
        :param input_cols: columns to be nested
        :param separator: char to be used as separator at the concat time
        :param shape: final data type, 'array', 'string' or 'vector'
        :param output_col:
        :return: Dask DataFrame
        """

        df = self.df
        input_cols = parse_columns(df, input_cols)
        # output_col = val_to_list(output_col)
        check_column_numbers(input_cols, 2)
        if output_col is None:
            RaiseIt.type_error(output_col, ["str"])

        # output_col = parse_columns(df, output_col, accepts_missing_cols=True)

        output_ordered_columns = df.cols.names()

        def _nest_string(row):
            v = row[input_cols[0]].astype(str)
            for i in builtins.range(1, len(input_cols)):
                v = v + separator + row[input_cols[i]].astype(str)
            return v

        def _nest_array(row):
            v = row[input_cols[0]].astype(str)
            for i in builtins.range(1, len(input_cols)):
                v += ", " + row[input_cols[i]].astype(str)
            return "[" + v + "]"

        if shape == "string":
            kw_columns = {output_col: _nest_string}
        else:
            kw_columns = {output_col: _nest_array}

        df = df.assign(**kw_columns)

        col_index = output_ordered_columns.index(input_cols[-1]) + 1
        output_ordered_columns[col_index:col_index] = [output_col]

        df = df.meta.preserve(df, Actions.NEST.value, list(kw_columns.values()))

        return df.cols.select(output_ordered_columns)

    def unnest(self, input_cols, separator=None, splits=2, index=None, output_cols=None, drop=False):

        """
        Split an array or string in different columns
        :param input_cols: Columns to be un-nested
        :param output_cols: Resulted on or multiple columns  after the unnest operation [(output_col_1_1,output_col_1_2), (output_col_2_1, output_col_2]
        :param separator: char or regex
        :param splits: Number of rows to un-nested. Because we can not know beforehand the number of splits
        :param index: Return a specific index per columns. [{1,2},()]
        :param drop:
        """
        # Special case. A dot must be escaped
        if separator == ".":
            separator = "\\."

        df = self.df

        input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)
        output_ordered_columns = df.cols.names()

        # columns = prepare_columns(df, input_cols, output_cols)
        # new data frame with split value columns

        for input_col in input_cols:
            df_new = df[input_col].astype("str").str.split(separator, n=splits)

            # Sometime when split the result are less parts that the split param. We handle this returning Null
            kw_columns = {}
            for i in range(splits):
                output_col = output_cols[0] + "_" + str(i)
                kw_columns[output_col] = df_new.map(lambda x, i=i: x[i] if i < len(x) else None)
                col_index = output_ordered_columns.index(input_col) + i + 1
                output_ordered_columns[col_index:col_index] = [output_col]

            df = df.assign(**kw_columns)

        if drop is True:
            df = df.drop(columns=input_cols)
            for input_col in input_cols:
                if input_col in output_ordered_columns: output_ordered_columns.remove(input_col)

        df = df.meta.preserve(df, Actions.UNNEST.value, list(kw_columns.keys()))

        return df.cols.select(output_ordered_columns)

    def replace(self, input_cols, search=None, replace_by=None, search_by="chars", ignore_case=False, output_cols=None):
        """
        Replace a value, list of values by a specified string
        :param input_cols: '*', list of columns names or a single column name.
        :param search: Values to look at to be replaced
        :param replace_by: New value to replace the old one
        :param search_by: Can be "full","words","chars" or "numeric".
        :param ignore_case: Ignore case when searching for match
        :param output_cols:
        :return: Dask DataFrame
        """

        df = self.df

        input_cols = parse_columns(df, input_cols)
        output_cols = get_output_cols(input_cols, output_cols)
        output_ordered_columns = df.cols.names()

        search = val_to_list(search)
        if search_by == "chars":
            str_regex = "|".join(map(re.escape, search))
        elif search_by == "words":
            str_regex = (r'\b%s\b' % r'\b|\b'.join(map(re.escape, search)))
        else:
            str_regex = search

        if ignore_case is True:
            # Cudf do not accept re.compile as argument for replace
            # regex = re.compile(str_regex, re.IGNORECASE)
            regex = str_regex
        else:
            regex = str_regex

        kw_columns = {}
        for input_col, output_col in zip(input_cols, output_cols):
            kw_columns[output_col] = df[input_col].str.replace(regex, replace_by)

            if input_col != output_col:
                col_index = output_ordered_columns.index(input_col) + 1
                output_ordered_columns[col_index:col_index] = [output_col]

        df = df.assign(**kw_columns)
        # The apply function seems to have problem appending new columns https://github.com/dask/dask/issues/2690
        # df = df.cols.apply(input_cols, _replace, func_return_type=str,
        #                    filter_col_by_dtypes=df.constants.STRING_TYPES,
        #                    output_cols=output_cols, args=(regex, replace_by))

        df = df.meta.preserve(df, Actions.REPLACE.value, one_list_to_val(output_cols))
        return df.cols.select(output_ordered_columns)

    def is_numeric(self, col_name):
        """
        Check if a column is numeric
        :param col_name:
        :return:
        """
        df = self.df
        # TODO: Check if this is the best way to check the data type
        if np.dtype(df[col_name]).type in [np.int64, np.int32, np.float64]:
            result = True
        else:
            result = False
        return result

    def select(self, columns="*", regex=None, data_type=None, invert=False, accepts_missing_cols=False):
        """
        Select columns using index, column name, regex to data type
        :param columns:
        :param regex: Regular expression to filter the columns
        :param data_type: Data type to be filtered for
        :param invert: Invert the selection
        :param accepts_missing_cols:
        :return:
        """
        df = self.df
        columns = parse_columns(df, columns, is_regex=regex, filter_by_column_dtypes=data_type, invert=invert,
                                accepts_missing_cols=accepts_missing_cols)
        if columns is not None:
            df = df[columns]
            result = df
        else:
            result = None

        return result

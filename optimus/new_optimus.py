from optimus.engines.base.meta import Meta


class PandasDataFrame:
    def __init__(self, df):
        self.data = df
        self.meta_data = {}

    def __getitem__(self, item):
        return self.cols.select(item)

    def __repr__(self):
        self.ext.display()
        return str(type(self))

    def new(self, df):
        return PandasDataFrame(df)

    @property
    def rows(self):
        from optimus.engines.pandas.rows import Rows
        return Rows(self)

    @property
    def cols(self):
        from optimus.engines.pandas.columns import Cols
        return Cols(self)

    @property
    def functions(self):
        from optimus.engines.pandas.functions import PandasFunctions
        return PandasFunctions(self)

    @property
    def ext(self):
        from optimus.engines.pandas.extension import Ext
        return Ext(self)

    @property
    def meta(self):
        return Meta(self)


class DaskDataFrame:
    def __init__(self, df):
        self.data = df
        self.meta_data = {}
        self.buffer = None

    def __getitem__(self, item):
        return self.cols.select(item)

    # def __repr__(self):
    #     self.ext.display()
    #     return str(type(self))

    def new(self, df, meta=None):
        new_df = DaskDataFrame(df)
        if meta is not None:
            new_df.meta.set(value=meta.meta.get())
        return new_df

    @property
    def rows(self):
        from optimus.engines.dask.rows import Rows
        return Rows(self)

    @property
    def cols(self):
        from optimus.engines.dask.columns import Cols
        return Cols(self)

    @property
    def functions(self):
        from optimus.engines.dask.functions import DaskFunctions
        return DaskFunctions(self)

    @property
    def ext(self):
        from optimus.engines.dask.extension import Ext
        return Ext(self)

    @property
    def meta(self):
        return Meta(self)


class SparkDataFrame:
    def __init__(self, df):
        self.data = df
        self.meta_data = {}

    def __getitem__(self, item):
        return self.cols.select(item)

    def __repr__(self):
        self.ext.display()
        return str(type(self))

    def new(self, df, meta=None):
        new_df = SparkDataFrame(df)
        if meta is not None:
            new_df.meta.set(value=meta.meta.get())
        return new_df

    @property
    def rows(self):
        from optimus.engines.spark.rows import Rows
        return Rows(self)

    @property
    def cols(self):
        from optimus.engines.spark.columns import Cols
        return Cols(self)

    @property
    def constants(self):
        from optimus.engines.spark.constants import Constants
        return Constants()

    @property
    def functions(self):
        from optimus.engines.spark.functions import SparkFunctions
        return SparkFunctions(self)

    @property
    def ext(self):
        from optimus.engines.spark.extension import Ext
        return Ext(self)

    @property
    def meta(self):
        return Meta(self)


class IbisDataFrame:
    def __init__(self, df):
        self.data = df
        self.meta_data = {}

    def __getitem__(self, item):
        return self.cols.select(item)

    # def __repr__(self):
    #     self.ext.display()
    #     return str(type(self))

    def new(self, df):
        return IbisDataFrame(df)

    @property
    def rows(self):
        from optimus.engines.ibis.rows import Rows
        return Rows(self)

    @property
    def cols(self):
        from optimus.engines.ibis.columns import Cols
        return Cols(self)

    @property
    def functions(self):
        from optimus.engines.ibis.functions import IbisFunctions
        return IbisFunctions(self)

    @property
    def ext(self):
        from optimus.engines.ibis.extension import Ext
        return Ext(self)

    @property
    def meta(self):
        return Meta(self)
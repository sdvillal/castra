from collections import Iterator
import os
from os import mkdir
from os.path import exists, isdir, join
try:
    import cPickle as pickle
except ImportError:
    import pickle
import shutil
import tempfile
from functools import partial

import blosc
import bloscpack
import numpy as np
import pandas as pd
from pandas import msgpack


bp_args = bloscpack.BloscpackArgs(offsets=False, checksum='None')

def blosc_args(dt):
    if np.issubdtype(dt, int):
        return bloscpack.BloscArgs(dt.itemsize, clevel=3, shuffle=True)
    if np.issubdtype(dt, np.datetime64):
        return bloscpack.BloscArgs(dt.itemsize, clevel=3, shuffle=True)
    if np.issubdtype(dt, float):
        return bloscpack.BloscArgs(dt.itemsize, clevel=1, shuffle=False)
    return None


def escape(text):
    return str(text)


def _safe_mkdir(path):
    if not exists(path):
        mkdir(path)


class Castra(object):
    def __init__(self, path=None, template=None, categories=None):
        # check if we should create a random path
        if path is None:
            self.path = tempfile.mkdtemp(prefix='castra-')
            self._explicitly_given_path = False
        else:
            self.path = path
            self._explicitly_given_path = True

        # check if the given path exists already and create it if it doesn't
        if not exists(self.path):
            mkdir(self.path)
        # raise an Exception if it isn't a directory
        elif not isdir(self.path):
            raise ValueError("'path': %s must be a directory")

        # either we have a meta directory
        if exists(self.dirname('meta')) and isdir(self.dirname('meta')):
            if template is not None:
                raise ValueError(
                    "'template' must be 'None' when opening a Castra")
            self.load_meta()
            self.load_partitions()
            self.load_categories()

        # or we don't, in which case we need a template
        elif template is not None:
            mkdir(self.dirname('meta'))
            mkdir(self.dirname('meta', 'categories'))
            self.columns, self.dtypes, self.index_dtype = \
                list(template.columns), template.dtypes, template.index.dtype
            self.partitions = pd.Series([], dtype='O',
                                        index=template.index.__class__([]))
            self.minimum = None
            if isinstance(categories, (list, tuple)):
                self.categories = dict((col, []) for col in categories)
            elif categories is True:
                self.categories = dict((col, [])
                                       for col in template.columns
                                       if template.dtypes[col] == 'object')
            else:
                self.categories = dict()

            self.flush_meta()
            self.save_partitions()
        else:
            raise ValueError(
                "must specify a 'template' when creating a new Castra")

    def load_meta(self, loads=pickle.loads):
        meta = []
        for name in ['columns', 'dtypes', 'index_dtype']:
            with open(self.dirname('meta', name), 'r') as f:
                meta.append(loads(f.read()))
        self.columns, self.dtype, self.index_dtype = meta

    def flush_meta(self, dumps=partial(pickle.dumps, protocol=2)):
        for name in ['columns', 'dtypes', 'index_dtype']:
            with open(self.dirname('meta', name), 'w') as f:
                f.write(dumps(getattr(self, name)))

    def load_partitions(self, loads=pickle.loads):
        with open(self.dirname('meta', 'plist'), 'r') as f:
            self.partitions = pickle.load(f)
        with open(self.dirname('meta', 'minimum'), 'r') as f:
            self.minimum = pickle.load(f)

    def save_partitions(self, dumps=partial(pickle.dumps, protocol=2)):
        with open(self.dirname('meta', 'minimum'), 'w') as f:
            f.write(dumps(self.minimum))
        with open(self.dirname('meta', 'plist'), 'w') as f:
            f.write(dumps(self.partitions))

    def append_categories(self, new, dumps=partial(pickle.dumps, protocol=2)):
        separator = '-sep-'
        for col, cat in new.items():
            if cat:
                with open(self.dirname('meta', 'categories', col), 'a') as f:
                    f.write(separator.join(map(dumps, cat)))
                    f.write(separator)

    def load_categories(self, loads=pickle.loads):
        separator = '-sep-'
        self.categories = dict()
        for col in self.columns:
            fn = self.dirname('meta', 'categories', col)
            if os.path.exists(fn):
                with open(fn) as f:
                    text = f.read()
                L = text.split(separator)[:-1]
                self.categories[col] = list(map(loads, L))

    def extend(self, df):
        # TODO: Ensure that df is consistent with existing data
        index = df.index.values
        partition_name = '--'.join([escape(index.min()), escape(index.max())])

        mkdir(self.dirname(partition_name))

        new_categories, self.categories, df = _decategorize(self.categories, df)
        self.append_categories(new_categories)

        # Store columns
        for col in df.columns:
            fn = self.dirname(partition_name, col)
            x = df[col].values
            pack_file(x, fn)

        # Store index
        fn = self.dirname(partition_name, '.index')
        x = df.index.values
        bloscpack.pack_ndarray_file(x, fn, bloscpack_args=bp_args,
                blosc_args=blosc_args(x.dtype))

        if len(self.partitions) == 0:
            self.minimum = index.min()
        self.partitions[index.max()] = partition_name
        self.flush()

    def dirname(self, *args):
        return os.path.join(self.path, *args)

    def load_partition(self, name, columns, categorize=True):
        if isinstance(columns, Iterator):
            columns = list(columns)
        if not isinstance(columns, list):
            df = self.load_partition(name, [columns], categorize=categorize)
            return df[df.columns[0]]
        arrays = [unpack_file(self.dirname(name, col))
                   for col in columns]
        index = unpack_file(self.dirname(name, '.index'))

        df = pd.DataFrame(dict(zip(columns, arrays)),
                            columns=columns,
                            index=pd.Index(index, dtype=self.index_dtype))
        if categorize:
            df = _categorize(self.categories, df)
        return df

    def __getitem__(self, key):
        if isinstance(key, tuple):
            key, columns = key
        else:
            columns = self.columns
        start, stop = key.start, key.stop
        names = select_partitions(self.partitions, key)

        data_frames = [self.load_partition(name, columns, categorize=False)
                       for name in names]

        data_frames[0] = data_frames[0].loc[start:]
        data_frames[-1] = data_frames[-1].loc[:stop]
        df = pd.concat(data_frames)
        df = _categorize(self.categories, df)
        return df

    def drop(self):
        if os.path.exists(self.path):
            shutil.rmtree(self.path)

    def flush(self):
        self.save_partitions()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if not self._explicitly_given_path:
            self.drop()
        else:
            self.flush()

    def __del__(self):
        if not self._explicitly_given_path:
            self.drop()
        else:
            self.flush()

    def __getstate__(self):
        self.flush()
        return (self.path, self._explicitly_given_path)

    def __setstate__(self, state):
        self.path = state[0]
        self._explicitly_given_path = state[1]
        self.load_meta()
        self.load_partitions()
        self.load_categories()

    def to_dask(self, columns=None):
        if columns is None:
            columns = self.columns
        import dask.dataframe as dd
        name = next(dd.core.names)
        dsk = dict(((name, i), (Castra.load_partition, self, part, columns))
                    for i, part in enumerate(self.partitions.values))
        divisions = [self.minimum] + list(self.partitions.index)
        if isinstance(columns, list):
            return dd.DataFrame(dsk, name, columns, divisions)
        else:
            return dd.Series(dsk, name, columns, divisions)


def pack_file(x, fn):
    """ Pack numpy array into filename

    Supports binary data with bloscpack and text data with msgpack+blosc

    >>> pack_file(np.array([1, 2, 3]), 'foo.blp')  # doctest: +SKIP

    See also:
        unpack_file
    """
    if x.dtype != 'O':
        bloscpack.pack_ndarray_file(x, fn, bloscpack_args=bp_args,
                blosc_args=blosc_args(x.dtype))
    else:
        bytes = blosc.compress(msgpack.packb(x.tolist()), 1)
        with open(fn, 'wb') as f:
            f.write(bytes)


def unpack_file(fn):
    """ Unpack numpy array from filename

    Supports binary data with bloscpack and text data with msgpack+blosc

    >>> unpack_file('foo.blp')  # doctest: +SKIP
    array([1, 2, 3])

    See also:
        pack_file
    """
    try:
        return bloscpack.unpack_ndarray_file(fn)
    except ValueError:
        with open(fn, 'rb') as f:
            bytes = f.read()
        return np.array(msgpack.unpackb(blosc.decompress(bytes)))


def coerce_index(dt, o):
    if np.issubdtype(dt, np.datetime64):
        return pd.Timestamp(o)
    return o


def select_partitions(partitions, key):
    """ Select partitions from partition list given slice

    >>> p = pd.Series(['a', 'b', 'c', 'd', 'e'], index=[0, 10, 20, 30, 40])
    >>> select_partitions(p, slice(3, 25))
    ['b', 'c', 'd']
    """
    assert key.step is None
    start, stop = key.start, key.stop
    names = list(partitions.loc[start:stop])

    last = partitions.searchsorted(names[-1])[0]

    stop2 = coerce_index(partitions.index.dtype, stop)
    if partitions.index[last] < stop2 and len(partitions) > last + 1:
        names.append(partitions.iloc[last + 1])

    return names


def _decategorize(categories, df):
    """ Strip object dtypes from dataframe, update categories

    Given a DataFrame

    >>> df = pd.DataFrame({'x': [1, 2, 3], 'y': ['C', 'B', 'B']})

    And a dict of known categories

    >>> _ = categories = {'y': ['A', 'B']}

    Update dict and dataframe in place

    >>> extra, categories, df = _decategorize(categories, df)
    >>> extra
    {'y': ['C']}
    >>> categories
    {'y': ['A', 'B', 'C']}
    >>> df
       x  y
    0  1  2
    1  2  1
    2  3  1
    """
    extra = dict()
    new_categories = dict()
    new_columns = dict((col, df[col]) for col in df.columns)
    for col, cat in categories.items():
        extra[col] = list(set(df[col]) - set(cat))
        new_categories[col] = cat + extra[col]
        new_columns[col] = pd.Categorical(df[col], new_categories[col]).codes
    new_df = pd.DataFrame(new_columns, columns=df.columns, index=df.index)
    return extra, new_categories, new_df


def _categorize(categories, df):
    """ Categorize columns in dataframe

    >>> df = pd.DataFrame({'x': [1, 2, 3], 'y': [0, 2, 0]})
    >>> categories = {'y': ['A', 'B', 'c']}
    >>> _categorize(categories, df)
       x  y
    0  1  A
    1  2  c
    2  3  A
    """
    if isinstance(df, pd.Series):
        if df.name in categories:
            cat = pd.Categorical.from_codes(df.values, categories[df.name])
            return pd.Series(cat, index=df.index)
        else:
            return df

    else:
        return pd.DataFrame(
                dict((col, pd.Categorical.from_codes(df[col], categories[col])
                           if col in categories
                           else df[col])
                    for col in df.columns),
                columns=df.columns,
                index=df.index)

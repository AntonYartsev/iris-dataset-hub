"""Kaggle source: every CSV / Parquet file of one small dataset version -> one table each, in one call

kagglehub downloads and unpacks the whole dataset version (and keeps it in its cache); this module
checks the version and its size first and then lays the files out as tables with the shared loader.
Files of other types are listed in the summary as not imported
"""
import logging
import os
import re

from dc_hub import loader
from dc_hub.loader import LoadError

MAX_TABLE_FILES = 100
# all files of the version, any type; kagglehub downloads them all
MAX_DATASET_FILES = 1000
MAX_LISTED = 20
LIST_PAGE_SIZE = 200

_HANDLE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_-]*)/([A-Za-z0-9][A-Za-z0-9_-]*)(?:/versions/([1-9][0-9]{0,8}))?")


def check_handle(handle):
    """owner/dataset or owner/dataset/versions/N -> (owner, dataset, N or None)"""
    if not handle:
        raise LoadError("handle is required: a Kaggle dataset such as 'owner/dataset/versions/1'")
    m = _HANDLE.fullmatch(handle)
    if not m or len(handle) > 200:
        raise LoadError("handle %r is not a Kaggle dataset handle of the form owner/dataset or "
                        "owner/dataset/versions/N" % handle)
    return m.group(1), m.group(2), int(m.group(3)) if m.group(3) else None


def latest_version(owner, dataset):
    """current version number of the dataset (metadata only, no data download)"""
    from kagglehub.clients import build_kaggle_client
    from kagglehub.exceptions import handle_call
    from kagglehub.handle import DatasetHandle
    from kagglesdk.datasets.types.dataset_api_service import ApiGetDatasetRequest
    request = ApiGetDatasetRequest()
    request.owner_slug, request.dataset_slug = owner, dataset
    with build_kaggle_client() as client:
        dataset_info = handle_call(lambda: client.datasets.dataset_api_client.get_dataset(request),
                                   DatasetHandle(owner=owner, dataset=dataset, version=None))
    return dataset_info.current_version_number


def check_size(owner, dataset, version):
    """kagglehub downloads the whole version as one archive, so every file of it counts
    (metadata only, no data download)"""
    from kagglehub.clients import build_kaggle_client
    from kagglehub.exceptions import handle_call
    from kagglehub.handle import DatasetHandle
    from kagglesdk.datasets.types.dataset_api_service import ApiListDatasetFilesRequest
    handle = DatasetHandle(owner=owner, dataset=dataset, version=version)
    request = ApiListDatasetFilesRequest()
    request.owner_slug, request.dataset_slug, request.dataset_version_number = owner, dataset, version
    request.page_size = LIST_PAGE_SIZE
    files = size = 0
    with build_kaggle_client() as client:
        while True:
            page = handle_call(lambda: client.datasets.dataset_api_client.list_dataset_files(request), handle)
            files += len(page.dataset_files)
            size += sum(f.total_bytes or 0 for f in page.dataset_files)
            if size > loader.MAX_FILE_BYTES:
                raise LoadError("version %d has more than %d MB of files; the whole version is downloaded, so this "
                                "loader is limited to small datasets" % (version, loader.MAX_FILE_BYTES // 2 ** 20))
            if files > MAX_DATASET_FILES:
                raise LoadError("version %d has more than %d files; this loader is meant for small datasets"
                                % (version, MAX_DATASET_FILES))
            if not page.next_page_token:
                return
            request.page_token = page.next_page_token


def dataset_files(root):
    """relative POSIX paths under root, sorted: (CSV / Parquet files, all other files)"""
    supported, other = [], []
    for folder, _, names in os.walk(root):
        for name in names:
            path = os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/")
            (supported if os.path.splitext(name)[1].lower() in loader.READERS else other).append(path)
    return sorted(supported), sorted(other)


def table_names(prefix, paths):
    """<prefix>_<path without extension>, normalized; collisions get _2, _3, ... in path order."""
    return loader.unique_names([loader.table_name("%s_%s" % (prefix, os.path.splitext(p)[0])) for p in paths])


def load_files(summary, root, prefix):
    """every CSV / Parquet file under root -> dc_hub_data.<prefix>_<file>, in sorted path order"""
    files, other = dataset_files(root)
    if not files:
        loader.fail(summary, LoadError("no CSV or Parquet files in this dataset version (files found: %d)" % len(other)))
    elif len(files) > MAX_TABLE_FILES:
        loader.fail(summary, LoadError("%d CSV / Parquet files, more than the %d table limit"
                                       % (len(files), MAX_TABLE_FILES)))
    else:
        loader.run(summary, [(f, name, lambda path=os.path.join(root, f): loader.read_file(path))
                             for f, name in zip(files, table_names(prefix, files))])
    if other:
        summary["notImported"] = loader.shortened(other, MAX_LISTED, "files")
    return summary


def load(summary, handle, prefix):
    """fills summary (a dict that holds the source); arguments are already normalized ("" = not given).
    An exception means the load failed before any table was touched"""
    summary.update(handle=handle, version=None, pinned=False)
    owner, dataset, version = check_handle(handle)
    summary["pinned"] = version is not None
    prefix = loader.check_target(prefix) if prefix else loader.table_name(dataset)
    import kagglehub
    logging.getLogger("kagglehub").setLevel(logging.WARNING)  # no download messages in the SQL output
    summary["version"] = version = version or latest_version(owner, dataset)
    check_size(owner, dataset, version)
    root = kagglehub.dataset_download("%s/%s/versions/%d" % (owner, dataset, version))
    load_files(summary, root, prefix)

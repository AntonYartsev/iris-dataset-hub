"""Hugging Face source: one config/split of a small flat Parquet or CSV dataset, no streaming

metadata is checked before the download (format, split, flat features, size); the downloaded
split is materialized as Arrow and handed to the shared loader. Values keep the types the
dataset declares: a column the dataset already typed as a number cannot get its original text back
"""
import os
import re

from dc_hub import loader
from dc_hub.loader import LoadError

FORMATS = ("parquet", "csv")
MAX_DATA_FILES = 100
MAX_LABELS = 20

_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_FILE_COMMIT = re.compile(r"@([0-9a-f]{40})/")


def check_ref(ref):
    if not ref:
        raise LoadError("ref is required: a Hugging Face dataset id such as 'hitorilabs/iris'")
    if not _REF.fullmatch(ref) or ".." in ref or len(ref) > 200 or os.path.exists(ref):
        raise LoadError("ref %r is not a Hugging Face dataset id of the form owner/name" % ref)


def default_table(ref, config, split):
    parts = [ref.split("/")[-1]] + ([config] if config and config != "default" else []) + [split]
    return loader.table_name("_".join(parts))


def _builder(ref, config, split, revision):
    """dataset metadata only (no data download); stops on anything this loader cannot take"""
    import datasets
    from huggingface_hub import HfFileSystem
    try:
        builder = datasets.load_dataset_builder(ref, config or None, revision=revision or None)
    except ValueError as e:
        if "Config name is missing" in str(e):
            raise LoadError("config is required (third argument of dc_hub.load_hf). " + str(e))
        raise
    if builder.name not in FORMATS:
        raise LoadError("the dataset files are read as %r; only Parquet and CSV datasets are supported" % builder.name)
    data_files = {str(s): list(files) for s, files in (builder.config.data_files or {}).items()}
    if split not in data_files:
        raise LoadError("split %r not found in config %r; available: %s"
                        % (split, builder.config.name, ", ".join(data_files) or "none"))
    if builder.info.features is not None:
        loader.check_schema(builder.info.features.arrow_schema)
    split_info = (builder.info.splits or {}).get(split)
    if split_info is not None and split_info.num_examples and split_info.num_examples > loader.MAX_ROWS:
        raise LoadError("split %r has %d rows, more than the %d row limit" % (split, split_info.num_examples, loader.MAX_ROWS))
    # load_dataset prepares every split of the config, so all of its files are downloaded
    files = [f for split_files in data_files.values() for f in split_files]
    if len(files) > MAX_DATA_FILES:
        raise LoadError("config %r has %d data files; this loader is meant for small datasets" % (builder.config.name, len(files)))
    fs = HfFileSystem()
    size = sum(fs.info(f)["size"] for f in files)
    if size > loader.MAX_FILE_BYTES:
        raise LoadError("the data files of config %r total %d bytes, more than the %d MB limit"
                        % (builder.config.name, size, loader.MAX_FILE_BYTES // 2 ** 20))
    commits = {m.group(1) for f in files for m in [_FILE_COMMIT.search(f)] if m}
    return builder, (commits.pop() if len(commits) == 1 else None)


def read_dataset(dataset):
    """datasets.Dataset -> TableData (honours an indices mapping, e.g. after select())"""
    return loader.read_arrow(dataset.with_format("arrow")[:])


def class_labels(features, data):
    """{SQL column: label names by code} for ClassLabel columns, which are stored as integer codes"""
    import datasets
    columns = {c.source: c.name for c in data.columns}
    out = {}
    for source, feature in features.items():
        if isinstance(feature, datasets.ClassLabel) and source in columns:
            out[columns[source]] = loader.shortened(list(feature.names), MAX_LABELS, "labels")
    return out


def load(summary, ref, split, config, target, revision):
    """fills summary (a dict that holds the source); arguments are already normalized ("" = not given).
    An exception means the load failed before any table was touched"""
    summary.update(ref=ref, config=config or None, split=split or None, revision=revision or None,
                   pinned=bool(_COMMIT.fullmatch(revision)))
    check_ref(ref)
    if not split:
        raise LoadError("split is required (second argument of dc_hub.load_hf), for example 'train'")
    name = loader.check_target(target) if target else default_table(ref, config, split)
    import datasets
    datasets.disable_progress_bars()
    builder, commit = _builder(ref, config, split, revision)
    summary["config"] = builder.config.name
    summary["commit"] = commit
    labels = {}

    def read():
        # the builder checked above downloads exactly the data files that were checked (load_dataset would
        # resolve the dataset again)
        builder.download_and_prepare()
        dataset = builder.as_dataset(split=split)
        data = read_dataset(dataset)
        labels.update(class_labels(dataset.features, data))
        return data

    loader.run(summary, [("%s/%s" % (builder.config.name, split), name, read)])
    if summary["status"] == "success" and labels:
        summary["classLabels"] = labels

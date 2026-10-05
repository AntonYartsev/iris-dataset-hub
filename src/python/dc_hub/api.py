"""logic behind the SQL functions of dc.hub.Api: SQL arguments in, short JSON summary out

a failed load is a summary with "status": "failed" and the error text; no exception reaches SQL.
Credentials come only from the environment and are masked in every summary
"""
import json
import os

SECRET_VARIABLES = ("HF_TOKEN", "KAGGLE_API_TOKEN", "KAGGLE_KEY")


def arg(value):
    """SQL NULL arrives as "" and SQL '' as "\\x00"; both mean "not given"."""
    value = "" if value is None else str(value)
    return "" if value == "\x00" else value.strip()


def to_json(summary):
    text = json.dumps(summary, ensure_ascii=False)
    for variable in SECRET_VARIABLES:
        secret = os.environ.get(variable, "")
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def _load(source, load, *args):
    from dc_hub import loader
    summary = {"source": source}
    try:
        load(summary, *[arg(v) for v in args])
    except Exception as e:  # the one place where a load becomes "failed"; the SQL call itself never fails
        loader.fail(summary, e)
    return to_json(summary)


def load_hf(ref, split, config, target, revision):
    from dc_hub import hf
    return _load("huggingface", hf.load, ref, split, config, target, revision)


def load_kaggle(handle, target_prefix):
    from dc_hub import kaggle
    return _load("kaggle", kaggle.load, handle, target_prefix)

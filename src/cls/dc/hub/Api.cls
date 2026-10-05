/// SQL entry points of iris-dataset-hub (schema dc_hub). Data goes only to schema dc_hub_data.
/// Each function returns a short JSON summary. A failed load returns "status": "failed" with the
/// error and the tables it touched instead of raising an SQL error
Class dc.hub.Api
{

/// SELECT dc_hub.load_hf(ref, split, config, target, revision)
/// Loads one split of a small flat Hugging Face Parquet/CSV dataset into dc_hub_data.<target>,
/// replacing that table. ref: owner/name; split: for example train; config: needed when the
/// dataset has several; target: table name (default <name>_<split>); revision: a commit pins the data
ClassMethod LoadHf(ref As %String(MAXLEN="") = "", split As %String(MAXLEN="") = "", config As %String(MAXLEN="") = "", target As %String(MAXLEN="") = "", revision As %String(MAXLEN="") = "") As %String(MAXLEN="") [ Language = python, SqlName = load_hf, SqlProc ]
{
    from dc_hub import api
    return api.load_hf(ref, split, config, target, revision)
}

/// SELECT dc_hub.load_kaggle(handle, targetPrefix)
/// Downloads one small Kaggle dataset version with kagglehub and loads every CSV/Parquet file into its
/// own table dc_hub_data.<targetPrefix>_<file>, replacing those tables. handle: owner/dataset/versions/N
/// (without /versions/N the latest version is used); targetPrefix: table name prefix (default: dataset name)
ClassMethod LoadKaggle(handle As %String(MAXLEN="") = "", targetPrefix As %String(MAXLEN="") = "") As %String(MAXLEN="") [ Language = python, SqlName = load_kaggle, SqlProc ]
{
    from dc_hub import api
    return api.load_kaggle(handle, targetPrefix)
}

}

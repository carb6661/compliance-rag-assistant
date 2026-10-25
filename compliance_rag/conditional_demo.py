"""Original, local-only proof of stale write/ABA guards; no old vulnerable service."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile

from .conditions import WriteCondition, WriteRejected
from .evaluation import DATA, sample_config, seed
from .knowledge import KnowledgeStore

NAME = "knowledge/v2.md"


def run():
    text = (DATA/"v2.md").read_bytes().decode("utf-8")
    def edit(store,content,condition):
        return store.ingest(content,NAME,sample_config("v2"),condition=condition)
    def tag(store):
        return WriteCondition.match(store.source(NAME)["source_etag"])
    def status(operation):
        try:
            operation()
            return 200
        except WriteRejected as error:
            return error.status
    with tempfile.TemporaryDirectory(prefix="compliance-rag-conditional-") as directory:
        store = KnowledgeStore(Path(directory)/"knowledge.sqlite3")
        seed(store)
        original = tag(store)
        revision = store.revision()
        missing = status(lambda: edit(store,text,None))
        created_over_existing = status(lambda: edit(store,text,WriteCondition.create()))
        assert store.revision() == revision
        a = status(lambda: edit(store,text.replace("180天","181天"),original))
        b = status(lambda: edit(store,text.replace("180天","182天"),original))
        assert [missing,created_over_existing,a,b] == [428,412,200,412]
        assert "181天" in store.source(NAME)["normalized_text"]
        edit(store,text,tag(store))
        content_aba = status(lambda: edit(store,text.replace("180天","182天"),original))
        assert content_aba == 412 and tag(store) != original
        before_delete = tag(store)
        store.delete(NAME,condition=before_delete)
        edit(store,text,WriteCondition.create())
        delete_aba = status(lambda: store.delete(NAME,condition=before_delete))
        assert delete_aba == 412 and tag(store) != before_delete
        shared = tag(store)
        before = store.revision()
        def competing(index):
            # Independent connections/objects with one reviewed version token.
            return status(lambda: edit(KnowledgeStore(store.path),text.replace("180天",f"{200+index}天"),shared))
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(competing,range(8)))
        assert statuses.count(200) == 1 and statuses.count(412) == 7
        assert store.revision() == before+1
        return {"dataset":"original-synthetic-source-write-v1","real_llm":False,
            "unconditional_update":missing,"create_over_existing":created_over_existing,
            "two_reviewers":{"first_update":a,"second_stale_update":b,"first_edit_preserved":True},
            "content_aba_old_tag":content_aba,"delete_recreate_old_tag":delete_aba,
            "threaded_writers":{"attempts":8,"committed":1,"conflicts":7,"revision_increments":1},
            "scope":"Actual 0.3 store behavior in private temporary SQLite; not an old-version exploit, LLM run or load benchmark."}


if __name__ == "__main__":
    print(json.dumps(run(),ensure_ascii=False,indent=2))

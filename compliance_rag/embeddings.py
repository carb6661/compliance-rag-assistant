"""Optional genuine Chinese ONNX embedding; never replace it with hash vectors."""
import hashlib
import importlib.metadata
from pathlib import Path
import threading

MODEL = "BAAI/bge-small-zh-v1.5"
REPOSITORY = "Qdrant/bge-small-zh-v1.5"
REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
FILES = ["model_optimized.onnx", "config.json", "tokenizer.json", "tokenizer_config.json",
         "special_tokens_map.json", "vocab.txt"]


def prepare_model(cache_dir=".models"):
    from huggingface_hub import snapshot_download
    return snapshot_download(REPOSITORY, revision=REVISION, cache_dir=cache_dir, allow_patterns=FILES)


class ChineseEmbedder:
    dimension = 512

    def __init__(self, model_path):
        from fastembed import TextEmbedding
        from tokenizers import Tokenizer
        directory = Path(model_path)
        digest = hashlib.sha256()
        for name in sorted(FILES):
            digest.update(name.encode())
            with (directory/name).open("rb") as stream:
                for block in iter(lambda: stream.read(1024*1024), b""):
                    digest.update(block)
        self.identity = f"{MODEL}:fastembed-{importlib.metadata.version('fastembed')}:{digest.hexdigest()}"
        self.model = TextEmbedding(MODEL, specific_model_path=str(directory), local_files_only=True, threads=2)
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.lock = threading.Lock()

    def _check(self, texts):
        from .parser import InputError
        if any(len(self.tokenizer.encode(text).ids) > 512 for text in texts):
            raise InputError("embedding input exceeds 512 tokens; rechunk explicitly, never silently truncate")

    def documents(self, texts):
        with self.lock:
            self._check(texts)
            return [v.tolist() for v in self.model.passage_embed(texts, batch_size=16)]

    def query(self, text):
        with self.lock:
            self._check([text])
            return next(iter(self.model.query_embed(text))).tolist()

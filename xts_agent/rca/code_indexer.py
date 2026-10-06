import os
import logging
from typing import List, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

class OEMCodeIndexer:
    def __init__(self, db_path: str, source_paths: List[str]):
        self.db_path = db_path
        self.source_paths = source_paths
        self.collection = None
        
    def _lazy_init(self):
        if self.collection is None:
            try:
                import chromadb
                from chromadb.utils import embedding_functions
                
                os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
                self.client = chromadb.PersistentClient(path=self.db_path)
                
                # Using sentence-transformers locally (no API keys needed)
                self.emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name="all-MiniLM-L6-v2"
                )
                
                self.collection = self.client.get_or_create_collection(
                    name="oem_source_code",
                    embedding_function=self.emb_fn
                )
            except ImportError as e:
                logger.error(f"Failed to import chromadb or sentence-transformers: {e}")
                raise
                
    def index_codebase(self):
        """Crawls source_paths and indexes C++/Java/Kotlin files."""
        self._lazy_init()
        logger.info(f"Starting indexing of OEM source paths: {self.source_paths}")
        
        valid_exts = {'.cpp', '.h', '.java', '.kt', '.c'}
        documents = []
        metadatas = []
        ids = []
        
        chunk_id = 0
        for base_path in self.source_paths:
            base_p = Path(base_path)
            if not base_p.exists():
                logger.warning(f"Source path {base_path} does not exist. Skipping.")
                continue
                
            for root, _, files in os.walk(base_path):
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in valid_exts:
                        filepath = os.path.join(root, file)
                        try:
                            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read()
                                
                            # Very basic chunking (by lines)
                            lines = content.split('\n')
                            chunk_size = 300
                            for i in range(0, len(lines), chunk_size):
                                chunk = '\n'.join(lines[i:i+chunk_size])
                                if len(chunk.strip()) > 50:
                                    documents.append(chunk)
                                    metadatas.append({"file": filepath, "start_line": i})
                                    ids.append(f"chunk_{chunk_id}")
                                    chunk_id += 1
                                    
                        except Exception as e:
                            logger.debug(f"Could not read {filepath}: {e}")
                            
        if documents:
            logger.info(f"Adding {len(documents)} code chunks to ChromaDB...")
            # ChromaDB batch size limits (typically 41666 max, let's chunk to 5000)
            batch_size = 5000
            for i in range(0, len(documents), batch_size):
                self.collection.add(
                    documents=documents[i:i+batch_size],
                    metadatas=metadatas[i:i+batch_size],
                    ids=ids[i:i+batch_size]
                )
            logger.info("Indexing complete.")
        else:
            logger.warning("No code found to index.")
            
    def search(self, query: str, top_k: int = 3) -> str:
        """Searches for relevant OEM code based on the crash log/query."""
        self._lazy_init()
        if self.collection.count() == 0:
            return "No OEM code indexed."
            
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=top_k
            )
            
            context = []
            for i in range(len(results['documents'][0])):
                doc = results['documents'][0][i]
                meta = results['metadatas'][0][i]
                context.append(f"--- File: {meta['file']} (Line {meta['start_line']}) ---\n{doc}\n")
                
            return "\n".join(context)
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return f"Search error: {str(e)}"

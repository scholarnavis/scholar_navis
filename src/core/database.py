import chromadb
from chromadb.config import Settings
import os
import logging
import traceback
from src.core.config_manager import ConfigManager

class DatabaseManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
            cls._instance.logger = logging.getLogger("Database")
            cls._instance.config_manager = ConfigManager()
            cls._instance.client = None
            cls._instance.collection = None
            cls._instance.embed_fn = None
            cls._instance.logger.debug(f"[DB_INIT] Instantiated new DatabaseManager! Memory address: {id(cls._instance)}")
        return cls._instance

    def switch_kb(self, kb_id, embedding_function=None, progress_callback=None):
        from src.core.kb_manager import KBManager
        kb_mgr = KBManager()
        kb_info = kb_mgr.get_kb_by_id(kb_id)
        if not kb_info: return False

        self.logger.info(f"Connecting to database: {kb_info['name']} (ID: {kb_id}) | Current instance: {id(self)}")

        if embedding_function is not None:
            self.embed_fn = embedding_function
        else:
            return False

        try:
            kb_root = kb_info['root_path']
            db_path = os.path.join(kb_root, "chroma_db")
            os.makedirs(db_path, exist_ok=True)

            if getattr(self, 'client', None) is not None:
                try:
                    self.client._system.stop()
                except:
                    pass

            self.client = None
            self.collection = None

            import chromadb
            try:
                chromadb.api.client.SharedSystemClient.clear_system_cache()
            except:
                pass

            self.client = chromadb.PersistentClient(
                path=db_path,
                settings=Settings(anonymized_telemetry=False, allow_reset=True)
            )

            self.collection = self.client.get_or_create_collection(
                name="main_collection",
                embedding_function=self.embed_fn,
                metadata={"kb_name": kb_info['name']}
            )

            if self.collection is None:
                self.collection = self.client.get_collection(name="main_collection", embedding_function=self.embed_fn)

            count = self.collection.count()
            self.logger.debug(f"DB Connected. Total documents: {count}")
            return True

        except Exception as e:
            self.logger.error(f"Connection crashed: {str(e)}\n{traceback.format_exc()}")
            return False

    def reload(self):
        self.client = None
        self.collection = None
        self.embed_fn = None
        import gc
        gc.collect()

    def add_documents(self, documents, metadatas, ids):
        self.logger.debug(f"Write request received! Preparing to write {len(documents)} vectors. Current instance: {id(self)}")

        if self.collection:
            try:
                sanitized_metadatas = []
                for meta in metadatas:
                    clean_meta = {}
                    for k, v in meta.items():
                        if v is None:
                            continue  # ChromaDB 拒绝 None 值
                        elif not isinstance(v, (str, int, float, bool)):
                            clean_meta[k] = str(v)
                        else:
                            clean_meta[k] = v
                    sanitized_metadatas.append(clean_meta)

                self.logger.debug("Calling ChromaDB underlying write API...")
                self.collection.add(
                    documents=documents,
                    metadatas=sanitized_metadatas, # 使用清洗后的元数据
                    ids=ids
                )
                self.logger.debug(f"Write successful! Current total in DB: {self.collection.count()}")
            except Exception as e:
                self.logger.error(f"Failed to add documents: {e}\n{traceback.format_exc()}")
                raise e
        else:
            self.logger.error("self.collection is None when trying to add documents!")

    def query(self, query_text, n_results=5):
        if self.collection:
            try:
                return self.collection.query(query_texts=[query_text], n_results=n_results)
            except Exception as e:
                self.logger.error(f"Query failed: {e}\n{traceback.format_exc()}")
                return None
        return None

    def delete_by_source(self, source_filename):
        if self.collection:
            try:
                self.collection.delete(where={"source": source_filename})
                self.logger.info(f"Successfully deleted documents originating from: {source_filename}")
                return True
            except Exception as e:
                self.logger.error(f"Failed to delete by source ({source_filename}): {e}")
                return False
        return False
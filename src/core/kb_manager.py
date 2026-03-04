import json
import logging
import os
import shutil
import traceback
import uuid
import datetime
import zipfile

from chromadb import Settings

from src.core.config_manager import ConfigManager
from src.core.theme_manager import ThemeManager


class KBManager:
    _instance = None
    WORKSPACE_DIR = None  # 移除 os.getcwd()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(KBManager, cls).__new__(cls)

            cls.WORKSPACE_DIR = ThemeManager.get_resource_path("scholar_workspace")

            if not os.path.exists(cls.WORKSPACE_DIR):
                os.makedirs(cls.WORKSPACE_DIR)
        return cls._instance

    def get_all_kbs(self):
        """扫描工作区，获取所有库的元数据"""
        kbs = []
        if not os.path.exists(self.WORKSPACE_DIR): return []

        for entry in os.scandir(self.WORKSPACE_DIR):
            if entry.is_dir():
                meta_path = os.path.join(entry.path, "meta.json")
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            # 动态计算
                            doc_dir = os.path.join(entry.path, "documents")
                            data['doc_count'] = len(os.listdir(doc_dir)) if os.path.exists(doc_dir) else 0
                            data['size_mb'] = self._get_dir_size(doc_dir)
                            data['root_path'] = entry.path
                            kbs.append(data)
                    except:
                        pass

        return sorted(kbs, key=lambda x: x.get('created_at', ''), reverse=True)

    def get_kb_by_id(self, kb_id):
        """直接获取指定 ID 的知识库元数据"""
        kb_root = os.path.join(self.WORKSPACE_DIR, kb_id)
        meta_path = os.path.join(kb_root, "meta.json")

        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    data['root_path'] = kb_root
                    return data
            except:
                return None
        return None

    def create_kb(self, name, desc, domain, model_id):

        # AUTO 自动解析
        if model_id == "embed_auto":
            from src.core.device_manager import DeviceManager
            from src.core.config_manager import ConfigManager
            from src.core.models_registry import resolve_auto_model

            dev_mgr = DeviceManager()
            config = ConfigManager()
            user_pref = config.user_settings.get("inference_device", "Auto")
            target_device = dev_mgr.parse_device_string(user_pref)
            model_id = resolve_auto_model("embedding", target_device)

        kb_id = str(uuid.uuid4())
        kb_root = os.path.join(self.WORKSPACE_DIR, kb_id)

        os.makedirs(os.path.join(kb_root, "documents"))
        os.makedirs(os.path.join(kb_root, "chroma_db"))


        from src.core.models_registry import get_model_conf
        model_conf = get_model_conf(model_id, "embedding")

        meta = {
            "id": kb_id,
            "name": name,
            "description": desc,
            "domain": domain,
            "model_id": model_id,
            "model_info": model_conf,
            "status": "ready",
            "file_map": {},
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        }

        with open(os.path.join(kb_root, "meta.json"), 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=4)

        return kb_id

    def set_kb_status(self, kb_id, status: str):

        kb_root = os.path.join(self.WORKSPACE_DIR, kb_id)
        meta_path = os.path.join(kb_root, "meta.json")

        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r+', encoding='utf-8') as f:
                    data = json.load(f)
                    data['status'] = status
                    data['updated_at'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                    f.seek(0)
                    json.dump(data, f, indent=4)
                    f.truncate()
            except Exception as e:
                import logging
                logging.error(f"Failed to update KB status for {kb_id}: {str(e)}")


    def update_kb_info(self, kb_id, name, desc, domain):
        kb_root = os.path.join(self.WORKSPACE_DIR, kb_id)
        meta_path = os.path.join(kb_root, "meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, 'r+') as f:
                data = json.load(f)
                data['name'] = name
                data['description'] = desc
                data['domain'] = domain
                data['updated_at'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                f.seek(0)
                json.dump(data, f, indent=4)
                f.truncate()

    def import_file_to_kb(self, kb_id, source_path):
        kb_root = os.path.join(self.WORKSPACE_DIR, kb_id)
        target_dir = os.path.join(kb_root, "documents")
        if not os.path.exists(source_path): return None

        original_filename = os.path.basename(source_path)

        # 生成混淆文件名 (无后缀的 UUID)
        obfuscated_name = str(uuid.uuid4())
        target_path = os.path.join(target_dir, obfuscated_name)

        # 复制文件并抹除后缀
        shutil.copy2(source_path, target_path)

        # 更新映射表
        meta_path = os.path.join(kb_root, "meta.json")
        try:
            with open(meta_path, 'r+', encoding='utf-8') as f:
                data = json.load(f)
                if 'file_map' not in data:
                    data['file_map'] = {}
                # 记录 UUID -> 真名
                data['file_map'][obfuscated_name] = original_filename
                data['updated_at'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                f.seek(0)
                json.dump(data, f, indent=4)
                f.truncate()
        except Exception as e:
            import logging
            logging.error(f"Failed to update file map: {str(e)}")

        # 返回目标路径和真实文件名，供 Task 传递给 ChromaDB
        return target_path, original_filename


    def delete_kb(self, kb_id):
        kb_root = os.path.join(self.WORKSPACE_DIR, kb_id)
        if os.path.exists(kb_root): shutil.rmtree(kb_root)

    def get_kb_files(self, kb_id):
        """根据混淆映射表还原展示给 UI 的文件列表"""
        kb_root = os.path.join(self.WORKSPACE_DIR, kb_id)
        doc_dir = os.path.join(kb_root, "documents")
        meta = self.get_kb_by_id(kb_id)
        file_map = meta.get('file_map', {}) if meta else {}

        files = []
        if os.path.exists(doc_dir):
            for obfuscated_name in os.listdir(doc_dir):
                fp = os.path.join(doc_dir, obfuscated_name)
                if os.path.isfile(fp):
                    size = os.path.getsize(fp) / 1024 / 1024
                    # 查找真名，找不到就暂时显示 UUID
                    real_name = file_map.get(obfuscated_name, obfuscated_name)
                    files.append({"name": real_name, "path": fp, "size": f"{size:.2f} MB"})
        return files


    def update_doc_count(self, kb_id, count):
        pass

    def _get_dir_size(self, path):
        total = 0
        if not os.path.exists(path): return 0
        for f in os.scandir(path):
            if f.is_file(): total += f.stat().st_size
        return round(total / 1024 / 1024, 2)

    def _touch_meta(self, kb_root):
        meta_path = os.path.join(kb_root, "meta.json")
        try:
            with open(meta_path, 'r+') as f:
                data = json.load(f)
                data['updated_at'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                f.seek(0)
                json.dump(data, f, indent=4)
                f.truncate()
        except:
            pass

    def export_kb(self, kb_id, dest_path):
        kb_root = os.path.join(self.WORKSPACE_DIR, kb_id)
        if not os.path.exists(kb_root): return "Error: KB not found."

        base_name = os.path.splitext(dest_path)[0]
        try:
            shutil.make_archive(base_name, 'zip', root_dir=kb_root)
            return f"Exported successfully to:\n{os.path.basename(base_name)}.zip"
        except Exception as e:
            return f"Export failed: {str(e)}"

    def import_kb_from_bundle(self, bundle_path):
        if not os.path.exists(bundle_path): return "Error: File not found."
        try:
            with zipfile.ZipFile(bundle_path, 'r') as zip_ref:
                if "meta.json" not in zip_ref.namelist(): return "Error: Invalid Project File."
                with zip_ref.open("meta.json") as f: meta = json.load(f)


            model_info = meta.get("model_info")
            if model_info:
                from src.core.models_registry import register_external_model
                register_external_model(model_info, "embedding")

            new_id = str(uuid.uuid4())
            new_root = os.path.join(self.WORKSPACE_DIR, new_id)

            with zipfile.ZipFile(bundle_path, 'r') as zip_ref:
                zip_ref.extractall(new_root)

            meta_path = os.path.join(new_root, "meta.json")
            with open(meta_path, 'r+', encoding='utf-8') as f:
                data = json.load(f)
                data['id'] = new_id
                data['status'] = "ready"
                f.seek(0)
                json.dump(data, f, indent=4)
                f.truncate()

            return f"Imported: {data.get('name', 'Unknown')}"
        except Exception as e:
            return f"Import failed: {str(e)}"

    def _update_meta_field(self, kb_id, field_name, field_value):
        kb_root = os.path.join(self.WORKSPACE_DIR, kb_id)
        meta_path = os.path.join(kb_root, "meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r+', encoding='utf-8') as f:
                    data = json.load(f)
                    data[field_name] = field_value
                    data['updated_at'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                    f.seek(0)
                    json.dump(data, f, indent=4)
                    f.truncate()
            except Exception as e:
                import logging
                logging.error(f"Failed to update meta field {field_name}: {str(e)}")


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
            cls._instance.logger.debug(f"Instantiated new DatabaseManager! Memory address: {id(cls._instance)}")
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

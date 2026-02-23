import os
import logging
import traceback
import uuid
import time
import gc

import pymupdf4llm
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownTextSplitter
from src.core.database import DatabaseManager


class PDFEngine:
    def __init__(self):
        self.logger = logging.getLogger("PDFEngine")
        self.logger.setLevel(logging.DEBUG)
        self.db = DatabaseManager()

        self.logger.debug(f"PDFEngine instantiated. Bound DB instance address: {id(self.db)}")

    def process_pdf(self, file_path, real_filename=None, chunk_size=1000, chunk_overlap=200, batch_size=32,
                    progress_callback=None):
        t_start = time.time()
        display_name = real_filename if real_filename else os.path.basename(file_path)

        self.logger.debug(f"Starting to parse: {display_name} | Physical path: {file_path}")

        if not os.path.exists(file_path):
            self.logger.error(f"File does not exist: {file_path}")
            return 0

        try:
            self.logger.debug("Calling PyMuPDF4LLM for Markdown conversion...")
            md_text = pymupdf4llm.to_markdown(file_path)

            if not md_text:
                self.logger.error(f"Parser returned empty data! File might be corrupted or unparseable: {display_name}")
                return 0

            total_raw_chars = len(md_text)
            self.logger.debug(
                f"Successfully loaded and converted to Markdown. Total {total_raw_chars} characters. Preparing to split...")

            # 2. Wrap into a Langchain Document object
            raw_doc = Document(page_content=md_text, metadata={"source": display_name})

            del md_text

            text_splitter = MarkdownTextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap
            )

            splits = text_splitter.split_documents([raw_doc])

            del raw_doc
            gc.collect()

            total_chunks = len(splits)
            self.logger.debug(f"Successfully split into {total_chunks} chunks.")

            if total_chunks == 0:
                self.logger.warning("Chunk count is 0 after splitting!")
                return 0

            creation_time = str(os.path.getctime(file_path))
            for doc in splits:
                doc.metadata.update({
                    "source": display_name,
                    "file_path": file_path,
                    "created_at": creation_time
                })

            processed_count = 0

            self.logger.debug(
                f"Preparing to inject into vector DB. Does current DB instance contain a collection?: {hasattr(self.db, 'collection') and self.db.collection is not None}")

            if hasattr(self.db, 'collection') and self.db.collection is not None:
                for i in range(0, total_chunks, batch_size):
                    batch_docs = splits[i: i + batch_size]
                    docs_texts = [doc.page_content for doc in batch_docs]
                    docs_metas = [doc.metadata for doc in batch_docs]
                    docs_ids = [str(uuid.uuid4()) for _ in batch_docs]

                    self.logger.debug(f"Sending Batch {i // batch_size + 1}: {len(batch_docs)} chunks to DB...")
                    self.db.add_documents(documents=docs_texts, metadatas=docs_metas, ids=docs_ids)

                    processed_count += len(batch_docs)
                    if progress_callback:
                        progress_callback(
                            int((processed_count / total_chunks) * 100),
                            f"Vectorizing: {processed_count}/{total_chunks}"
                        )
            else:
                self.logger.warning("self.db.collection is missing inside PDFEngine! Data discarded!")
                return 0

            self.logger.info(f"File {display_name} processed successfully, time elapsed: {time.time() - t_start:.2f}s")
            return total_chunks

        except Exception as e:
            self.logger.error(f"Fatal exception occurred:\n{traceback.format_exc()}")
            return 0
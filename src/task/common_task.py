from src.core.core_task import BackgroundTask
from src.core.network_worker import create_robust_session


class VersionCheckTask(BackgroundTask):
    def _execute(self):
        try:
            session = create_robust_session()
            response = session.get("https://scholarnavis.com/latest", timeout=5)
            if response.status_code == 200:
                latest_version = response.text.strip()
                return {"latest_version": latest_version}
        except Exception as e:
            self.logger.error(f"Failed to check for updates: {e}")
        return {"latest_version": None}

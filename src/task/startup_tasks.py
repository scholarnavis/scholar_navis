from src.core.core_task import BackgroundTask

class HardwareInitTask(BackgroundTask):
    def _execute(self):
        self.update_progress(10, "Initializing hardware detection...")
        from src.core.device_manager import DeviceManager
        dev_mgr = DeviceManager()
        dev_mgr.get_optimal_device()
        self.update_progress(100, "Hardware detection complete.")
        return True
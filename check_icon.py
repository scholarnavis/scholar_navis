import os
import ctypes
from PySide6.QtGui import QIcon, QImageReader
from PySide6.QtWidgets import QApplication
import sys


def check_icon_health(ico_path):
    print(f"🕵️ 开始诊断图标: {os.path.abspath(ico_path)}\n")

    # 1. 检查文件是否存在
    if not os.path.exists(ico_path):
        print("❌ 致命错误: 文件根本不存在！请检查路径。")
        return

    # 2. 检查文件头部魔数 (Magic Number)，抓出“改后缀名”的李鬼
    try:
        with open(ico_path, 'rb') as f:
            header = f.read(4)
            if header == b'\x89PNG':
                print("❌ 真凶找到: 这是一个披着 .ico 外衣的 PNG 文件！Windows 任务栏绝对不认！")
                print("   👉 解决办法: 请使用在线工具 (如 png2ico) 将其真正转换为 ICO 格式。")
                return
            elif header != b'\x00\x00\x01\x00':
                print(f"❌ 真凶找到: 文件头部字节非法 ({header})，这不是一个标准的 Windows ICO 文件！")
                return
            else:
                print("✅ 格式校验: 文件头部是合法的 ICO 格式。")
    except Exception as e:
        print(f"❌ 读取文件出错: {e}")
        return

    # 3. 逼问 Windows 操作系统底层 API
    print("⏳ 正在呼叫 Windows 底层 API (LoadImageW)...")
    LR_LOADFROMFILE = 0x0010
    IMAGE_ICON = 1

    # 调用 Windows User32.dll 尝试加载图标
    hIcon = ctypes.windll.user32.LoadImageW(
        0,
        os.path.abspath(ico_path),
        IMAGE_ICON,
        0, 0,
        LR_LOADFROMFILE
    )

    if hIcon == 0:
        error_code = ctypes.windll.kernel32.GetLastError()
        print(f"❌ Windows 拒绝加载此图标！系统抛出错误码: {error_code}")
        if error_code == 0:
            print("   👉 错误码 0 通常意味着文件解析失败（比如只包含不受支持的尺寸，或文件损坏）。")
        return
    else:
        print("✅ Windows API 测试: Windows 操作系统可以完美识别并加载此图标。")
        # 释放句柄防内存泄漏
        ctypes.windll.user32.DestroyIcon(hIcon)

    # 4. 逼问 PySide6 / Qt 引擎
    app = QApplication.instance() or QApplication(sys.argv)
    icon = QIcon(ico_path)
    if icon.isNull():
        print("❌ PySide6 测试: Windows 能认，但 PySide6 认为这是一个无效的图标！(可能缺少 imageformats 插件)")
    else:
        sizes = icon.availableSizes()
        print(f"✅ PySide6 测试: 加载成功！图标内部包含的尺寸层级有: {sizes}")
        if not sizes:
            print("⚠️ 警告: 图标被加载，但内部没有任何尺寸数据，任务栏依然可能显示空白！")

    print("\n🎉 诊断结论:")
    print("如果上面全部打 ✅，说明你的图标文件完美无瑕。")
    print("如果此时任务栏还是没图标，那 100% 是 Windows 缓存没清干净，或者你的 AppUserModelID (AUMID) 注册代码被跳过了！")


if __name__ == "__main__":
    # 替换为你实际的图标路径
    test_path = os.path.join("Assets", "icon.ico")
    check_icon_health(test_path)
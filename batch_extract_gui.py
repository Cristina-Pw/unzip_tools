import os
import sys
import threading
import queue
import zipfile
import rarfile
import py7zr
from pathlib import Path
from tkinter import *
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- 设置 RAR 解压工具路径（如需手动指定）----------
# rarfile.UNRAR_TOOL = "C:/Program Files/WinRAR/UnRAR.exe"

SUPPORTED_EXTS = {'.zip', '.rar', '.7z'}

# ---------- 解压核心函数（带日志回调）----------
def extract_zip(archive_path, output_dir, password, overwrite, log_func):
    try:
        with zipfile.ZipFile(archive_path, 'r') as zf:
            pwd = password.encode('utf-8') if password else None
            # 验证密码（读取第一个加密文件）
            try:
                if zf.infolist():
                    zf.read(zf.infolist()[0], pwd=pwd)
            except RuntimeError as e:
                if "Bad password" in str(e) or "encrypted" in str(e):
                    return False, "密码错误"
            except Exception:
                pass

            for member in zf.infolist():
                target_path = output_dir / member.filename
                if target_path.exists() and not overwrite:
                    log_func(f"  跳过已存在: {member.filename}")
                    continue
                if target_path.resolve().relative_to(output_dir.resolve()):
                    if member.is_dir():
                        target_path.mkdir(parents=True, exist_ok=True)
                    else:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member, pwd=pwd) as src, open(target_path, 'wb') as dst:
                            dst.write(src.read())
                else:
                    log_func(f"  警告：跳过非法路径 {member.filename}")
        return True, "解压成功"
    except zipfile.BadZipFile:
        return False, "损坏或非 ZIP 文件"
    except RuntimeError as e:
        if "Bad password" in str(e) or "encrypted" in str(e):
            return False, "密码错误"
        return False, f"解压出错: {e}"
    except Exception as e:
        return False, f"未知错误: {e}"

def extract_rar(archive_path, output_dir, password, overwrite, log_func):
    try:
        with rarfile.RarFile(archive_path, 'r') as rf:
            rf.setpassword(password)
            for member in rf.infolist():
                target_path = output_dir / member.filename
                if target_path.exists() and not overwrite:
                    log_func(f"  跳过已存在: {member.filename}")
                    continue
                if target_path.resolve().relative_to(output_dir.resolve()):
                    if member.isdir():
                        target_path.mkdir(parents=True, exist_ok=True)
                    else:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        with rf.open(member, pwd=password) as src, open(target_path, 'wb') as dst:
                            dst.write(src.read())
                else:
                    log_func(f"  警告：跳过非法路径 {member.filename}")
        return True, "解压成功"
    except rarfile.RarCannotExec:
        return False, "未找到 unrar 工具，请安装 WinRAR 或 unrar"
    except rarfile.RarBadPassword:
        return False, "密码错误"
    except rarfile.BadRarFile:
        return False, "损坏或非 RAR 文件"
    except Exception as e:
        return False, f"未知错误: {e}"

def extract_7z(archive_path, output_dir, password, overwrite, log_func):
    try:
        with py7zr.SevenZipFile(archive_path, mode='r', password=password) as sz:
            all_files = sz.getnames()
            for name in all_files:
                target_path = output_dir / name
                if target_path.exists() and not overwrite:
                    log_func(f"  跳过已存在: {name}")
                    continue
                if target_path.resolve().relative_to(output_dir.resolve()):
                    if name.endswith('/'):
                        target_path.mkdir(parents=True, exist_ok=True)
                    else:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        sz.extract(targets=[name], path=output_dir)
                else:
                    log_func(f"  警告：跳过非法路径 {name}")
        return True, "解压成功"
    except py7zr.exceptions.Bad7zFile:
        return False, "损坏或非 7Z 文件"
    except py7zr.exceptions.PasswordRequired:
        return False, "需要密码（未提供）"
    except py7zr.exceptions.WrongPassword:
        return False, "密码错误"
    except Exception as e:
        return False, f"未知错误: {e}"

EXTRACTORS = {
    '.zip': extract_zip,
    '.rar': extract_rar,
    '.7z': extract_7z,
}

def process_archive(archive_path, output_dir, password, overwrite, input_dir, log_func):
    """处理单个压缩包，并实时记录日志"""
    suffix = archive_path.suffix.lower()
    if suffix not in EXTRACTORS:
        log_func(f"⚠️ 不支持的格式: {archive_path.name}")
        return False

    # 计算相对路径
    try:
        rel_path = archive_path.relative_to(input_dir)
    except ValueError:
        rel_path = archive_path.name
    target_dir = output_dir / rel_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    log_func(f"⏳ 正在解压: {archive_path.name}")
    extract_func = EXTRACTORS[suffix]
    success, msg = extract_func(archive_path, target_dir, password, overwrite, log_func)
    if success:
        log_func(f"✅ {archive_path.name}: {msg}")
    else:
        log_func(f"❌ {archive_path.name}: {msg}")
    return success

# ---------- GUI 应用程序 ----------
class BatchExtractApp:
    def __init__(self, root):
        self.root = root
        root.title("批量解压工具")
        root.geometry("700x600")
        root.resizable(True, True)

        # 变量
        self.input_dir = StringVar()
        self.output_dir = StringVar()
        self.password = StringVar()
        self.no_password = BooleanVar(value=False)
        self.overwrite = BooleanVar(value=False)
        self.ext_zip = BooleanVar(value=True)
        self.ext_rar = BooleanVar(value=True)
        self.ext_7z = BooleanVar(value=True)

        # 日志队列（用于线程安全更新）
        self.log_queue = queue.Queue()
        self.running = False

        # 创建界面
        self.create_widgets()

        # 定时检查日志队列
        self.poll_log_queue()

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=BOTH, expand=True)

        # ---------- 输入目录 ----------
        row1 = ttk.Frame(main_frame)
        row1.pack(fill=X, pady=5)
        ttk.Label(row1, text="压缩包文件夹:").pack(side=LEFT)
        ttk.Entry(row1, textvariable=self.input_dir, width=50).pack(side=LEFT, padx=5, fill=X, expand=True)
        ttk.Button(row1, text="浏览...", command=self.browse_input).pack(side=RIGHT)

        # ---------- 输出目录 ----------
        row2 = ttk.Frame(main_frame)
        row2.pack(fill=X, pady=5)
        ttk.Label(row2, text="输出文件夹:").pack(side=LEFT)
        ttk.Entry(row2, textvariable=self.output_dir, width=50).pack(side=LEFT, padx=5, fill=X, expand=True)
        ttk.Button(row2, text="浏览...", command=self.browse_output).pack(side=RIGHT)

        # ---------- 密码 ----------
        row3 = ttk.Frame(main_frame)
        row3.pack(fill=X, pady=5)
        ttk.Label(row3, text="解压密码:").pack(side=LEFT)
        ttk.Entry(row3, textvariable=self.password, width=30, show="*").pack(side=LEFT, padx=5)
        ttk.Checkbutton(row3, text="无密码", variable=self.no_password, command=self.toggle_password).pack(side=LEFT, padx=10)

        # ---------- 选项 ----------
        row4 = ttk.Frame(main_frame)
        row4.pack(fill=X, pady=5)
        ttk.Checkbutton(row4, text="覆盖已存在文件", variable=self.overwrite).pack(side=LEFT, padx=5)
        ttk.Label(row4, text="  扩展名:").pack(side=LEFT, padx=(20,5))
        ttk.Checkbutton(row4, text=".zip", variable=self.ext_zip).pack(side=LEFT)
        ttk.Checkbutton(row4, text=".rar", variable=self.ext_rar).pack(side=LEFT)
        ttk.Checkbutton(row4, text=".7z", variable=self.ext_7z).pack(side=LEFT)

        # ---------- 开始按钮 ----------
        self.btn_start = ttk.Button(main_frame, text="开始解压", command=self.start_extract)
        self.btn_start.pack(pady=10)

        # ---------- 日志文本框 ----------
        log_frame = ttk.LabelFrame(main_frame, text="解压日志", padding="5")
        log_frame.pack(fill=BOTH, expand=True, pady=5)
        self.log_text = Text(log_frame, wrap=WORD, height=20)
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side=RIGHT, fill=Y)
        self.log_text.config(yscrollcommand=scrollbar.set)

        # 设置输出目录默认值（在输入目录后自动填充）
        self.input_dir.trace('w', self.auto_output)

    def toggle_password(self):
        if self.no_password.get():
            self.password.set("")
            self.password.config(state=DISABLED)
        else:
            self.password.config(state=NORMAL)

    def auto_output(self, *args):
        if self.input_dir.get() and not self.output_dir.get():
            self.output_dir.set(str(Path(self.input_dir.get()) / "extracted"))

    def browse_input(self):
        path = filedialog.askdirectory(title="选择压缩包所在文件夹")
        if path:
            self.input_dir.set(path)

    def browse_output(self):
        path = filedialog.askdirectory(title="选择输出文件夹")
        if path:
            self.output_dir.set(path)

    def log(self, msg):
        """将消息放入队列，由主线程轮询显示"""
        self.log_queue.put(msg)

    def poll_log_queue(self):
        """主线程定时检查日志队列并更新文本框"""
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.insert(END, msg + "\n")
                self.log_text.see(END)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.poll_log_queue)

    def start_extract(self):
        if self.running:
            messagebox.showinfo("提示", "正在解压中，请稍候...")
            return

        input_dir = Path(self.input_dir.get().strip())
        if not input_dir.is_dir():
            messagebox.showerror("错误", "请选择有效的压缩包文件夹")
            return

        output_dir = Path(self.output_dir.get().strip()) if self.output_dir.get() else input_dir / "extracted"
        output_dir.mkdir(parents=True, exist_ok=True)

        password = None if self.no_password.get() else self.password.get().strip()
        if not self.no_password.get() and not password:
            if not messagebox.askyesno("密码为空", "您未输入密码，是否继续（尝试无密码解压）？"):
                return

        # 收集选择的扩展名
        extensions = []
        if self.ext_zip.get():
            extensions.append('.zip')
        if self.ext_rar.get():
            extensions.append('.rar')
        if self.ext_7z.get():
            extensions.append('.7z')
        if not extensions:
            messagebox.showerror("错误", "请至少选择一种压缩格式")
            return

        overwrite = self.overwrite.get()

        # 清空日志
        self.log_text.delete(1.0, END)
        self.log("🔄 开始扫描压缩包...")

        # 启动后台线程
        self.running = True
        self.btn_start.config(state=DISABLED)
        threading.Thread(target=self.extract_worker,
                         args=(input_dir, output_dir, password, extensions, overwrite),
                         daemon=True).start()

    def extract_worker(self, input_dir, output_dir, password, extensions, overwrite):
        """后台工作线程：执行解压"""
        try:
            # 查找所有压缩包
            archives = []
            for ext in extensions:
                archives.extend(Path(input_dir).rglob(f'*{ext}'))
            if not archives:
                self.log("⚠️ 未找到任何匹配的压缩包")
                self.finish()
                return

            self.log(f"📂 找到 {len(archives)} 个压缩包，开始解压...")

            success_count = 0
            fail_count = 0

            with ThreadPoolExecutor(max_workers=4) as executor:
                future_to_archive = {
                    executor.submit(process_archive, arch, output_dir, password, overwrite, input_dir, self.log): arch
                    for arch in archives
                }
                for future in as_completed(future_to_archive):
                    arch = future_to_archive[future]
                    try:
                        result = future.result()
                        if result:
                            success_count += 1
                        else:
                            fail_count += 1
                    except Exception as e:
                        self.log(f"❌ {arch.name}: 处理异常 - {e}")
                        fail_count += 1

            self.log(f"\n✅ 解压完成！成功 {success_count} 个，失败 {fail_count} 个")
        except Exception as e:
            self.log(f"❌ 发生错误: {e}")
        finally:
            self.finish()

    def finish(self):
        """恢复界面状态"""
        self.running = False
        self.root.after(0, lambda: self.btn_start.config(state=NORMAL))

# ---------- 主程序 ----------
if __name__ == "__main__":
    root = Tk()
    app = BatchExtractApp(root)
    root.mainloop()

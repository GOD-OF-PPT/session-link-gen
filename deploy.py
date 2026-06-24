"""
Deploy script: compile Python sources to bytecode (.pyc) and clean up.
Run this on the server after setting API_SECRET environment variable.

Usage:
    set API_SECRET=your-secret-key-here   (Windows)
    export API_SECRET=your-secret-key-here (Linux/Mac)

    python deploy.py
    waitress-serve app:app                (Windows production)
    gunicorn -w 4 -b 0.0.0.0:5000 app:app (Linux production)
"""
import os, sys, py_compile, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_FILES = ["core.py", "app.py"]

print("=" * 50)
print("Session Link Generator - Deploy")
print("=" * 50)

# 1. Check API_SECRET
api_secret = os.environ.get("API_SECRET", "").strip()
if api_secret:
    print(f"✅ API_SECRET 已设置")
else:
    print("⚠️  API_SECRET 未设置！")
    print("   部署后任何人都能调用 API。")
    print("   建议: set API_SECRET=你的密钥 (Windows)")
    print("         export API_SECRET=你的密钥 (Linux)")
    if input("   继续部署? (y/n): ").strip().lower() != "y":
        sys.exit(1)

# 2. Compile source files
print()
print("📦 编译 .py → .pyc ...")
cache_dir = ROOT / "__pycache__"
cache_dir.mkdir(exist_ok=True)

import compileall
for src in SRC_FILES:
    src_path = ROOT / src
    if not src_path.exists():
        print(f"   ❌ {src} 不存在，跳过")
        continue
    # Compile to __pycache__
    py_compile.compile(str(src_path), cfile=str(cache_dir / f"{Path(src).stem}.cpython-312.pyc"), dfile=src)
    print(f"   ✅ {src} → __pycache__/{Path(src).stem}.cpython-312.pyc")

# 3. Rename .py files to .py.bak (only on server, not local dev)
print()
print("🔒 保护源码 ...")
backup = []
for src in SRC_FILES:
    src_path = ROOT / src
    bak_path = ROOT / f"{src}.bak"
    if src_path.exists():
        src_path.rename(bak_path)
        print(f"   ✅ {src} → {src}.bak (已隐藏)")
        backup.append((bak_path, src_path))

print()
print("=" * 50)
print("✅ 部署完成！")
print()
print("启动服务:")
print("  pip install waitress")
print("  waitress-serve app:app")
print()
print("前端访问: http://你的IP:5000")
print("API 调用需要在请求头添加: X-API-Key: 你的密钥")
print()
print("如需恢复源文件: python deploy_restore.py")
print("=" * 50)

# 4. Write restore script
restore_script = ROOT / "deploy_restore.py"
restore_script.write_text("""\
\"\"\"Restore .py source files from .py.bak backups.\"\"\"
from pathlib import Path
ROOT = Path(__file__).resolve().parent
for bak in sorted(ROOT.glob("*.py.bak")):
    orig = ROOT / bak.name.replace(".py.bak", ".py")
    bak.rename(orig)
    print(f"✅ {bak.name} → {orig.name} (已恢复)")
print("完成！")
""", encoding="utf-8")
print("   ✅ deploy_restore.py 已生成")

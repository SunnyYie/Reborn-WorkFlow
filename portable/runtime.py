"""Installation, diagnostics and safe forwarding; business rules stay in workflow/."""

import argparse
import getpass
import importlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import venv
import xml.etree.ElementTree as XML
import zipfile

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "skills/pco-assemble-worship-setlist/assets/reference.docx"
DATABASE = ROOT / "rag/data/songs.sqlite3"


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def python_path():
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def workflow_imports():
    path = str(ROOT / "workflow")
    if path not in sys.path:
        sys.path.insert(0, path)


def diagnose(online=False):
    dependencies = {}
    for name in ("docx", "lxml.etree", "pypdf", "keyring", "mcp"):
        try:
            module = importlib.import_module(name)
            if name == "lxml.etree":
                module.fromstring(b"<ok/>")
            dependencies[name] = True
        except (ImportError, ValueError, OSError):
            dependencies[name] = False
    try:
        XML.fromstring("<ok/>")
        dependencies["xml"] = True
    except (ValueError, OSError):
        dependencies["xml"] = False
    tools = {name: shutil.which(name) is not None for name in ("ffprobe", "ffmpeg")}
    auth = {"status": "not_checked", "credentials_logged": False}
    if online:
        workflow_imports()
        from pco_api import PCO, WorkflowError
        try:
            PCO().request("/services/v2/songs?per_page=1")
            auth["status"] = "verified"
        except WorkflowError as error:
            auth.update(status=error.code, message=str(error))
    missing = []
    if sys.version_info < (3, 10):
        missing.append({"action": "provision_python_3_10_or_newer", "requires_authorization": True})
    if not all(dependencies[n] for n in ("docx", "lxml.etree", "pypdf", "keyring", "xml")):
        missing.append({"action": "setup", "requires_authorization": True})
    if not tools["ffprobe"]:
        missing.append({"action": "install_audio_tools", "requires_authorization": True})
    if not TEMPLATE.is_file():
        missing.append({"action": "import_authorized_template", "requires_user_material": True})
    if not DATABASE.is_file():
        missing.append({"action": "restore_library_or_init_empty_library", "requires_authorization": True})
    if auth["status"] != "verified":
        missing.append({"action": "verify_pco_access" if not online else "configure_or_unlock_pco",
                        "requires_authorization": True})
    return {
        "platform": platform.system(), "python": platform.python_version(),
        "dependencies": dependencies, "tools": tools,
        "template_present": TEMPLATE.is_file(), "library_present": DATABASE.is_file(),
        "pco": auth, "next_actions": missing,
        "local_prerequisites_ready": not missing,
        "native_document_review": "requires_connected_WPS_or_Word_and_actual_inspection",
        "agent_execution": "requires_local_execution_or_connected_MCP_host",
        "mcp_available": dependencies["mcp"],
        "notice": "Dependency availability is not end-to-end acceptance.",
    }


def install_system_tools():
    if shutil.which("ffprobe") and shutil.which("ffmpeg"):
        return
    system = platform.system()
    if system == "Windows" and shutil.which("winget"):
        command = ["winget", "install", "--id", "Gyan.FFmpeg", "--exact",
                   "--accept-source-agreements", "--accept-package-agreements"]
    elif system == "Darwin" and shutil.which("brew"):
        command = ["brew", "install", "ffmpeg"]
    else:
        raise ValueError("No supported package manager found. An administrator must provision FFmpeg/ffprobe.")
    subprocess.run(command, check=True)


def setup(with_mcp=False, system_tools=False):
    if sys.version_info < (3, 10):
        raise ValueError("The portable adapter requires Python 3.10+; use Python 3.12 when installing.")
    destination = ROOT / ".venv"
    if destination.exists() and not (destination / "pyvenv.cfg").is_file():
        raise ValueError("Existing .venv is not a virtual environment; it will not be overwritten.")
    if not python_path().is_file():
        venv.EnvBuilder(with_pip=True).create(destination)
    requirements = ROOT / ("requirements-agent.txt" if with_mcp else "requirements.txt")
    subprocess.run([str(python_path()), "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-cache-dir", "-r", str(requirements)], check=True)
    if system_tools:
        install_system_tools()
    emit({"installed": True, "python": str(python_path()), "with_mcp": with_mcp,
          "next": "doctor --online", "notice": "Open a new terminal if a system tool is not yet on PATH."})


def init_library():
    if DATABASE.exists():
        return {"created": False, "preserved_existing": True}
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("portable_rag", ROOT / "rag/rag.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # An empty valid library permits PCO-first material import, not historical reuse.
    with module.connect(DATABASE, create=True) as db:
        db.commit()
    return {"created": True, "path": str(DATABASE), "historical_songs": 0}


def import_template(source):
    source = Path(source).resolve()
    if TEMPLATE.exists():
        raise ValueError("A template already exists; it will not be overwritten.")
    if source.suffix.lower() != ".docx" or not zipfile.is_zipfile(source):
        raise ValueError("Choose an authorized DOCX template, not a generated substitute.")
    with zipfile.ZipFile(source) as archive:
        if not {"word/document.xml", "word/styles.xml"} <= set(archive.namelist()):
            raise ValueError("The template has no Word document/styles.")
    TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
    with TEMPLATE.open("xb") as dest, source.open("rb") as original:
        shutil.copyfileobj(original, dest)
    return {"imported": True, "path": str(TEMPLATE),
            "notice": "Template layout compatibility still requires export and native visual review."}


def configure_auth():
    if not sys.stdin.isatty():
        raise ValueError("Configure credentials in a local interactive terminal, never via agent/chat inputs.")
    workflow_imports()
    import credential_store
    client_id = getpass.getpass("PCO application ID (hidden): ").strip()
    secret = getpass.getpass("PCO secret (hidden, never paste into chat): ").strip()
    credential_store.save(client_id, secret)
    emit({"stored": True, "credentials_logged": False, "next": "doctor --online"})


def approve(question):
    return input(question + " [y/N] ").strip().lower() == "y"


def choose_template():
    if platform.system() == "Darwin" and shutil.which("osascript"):
        result = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose file with prompt "Choose your authorized Word template" '
             'of type {"org.openxmlformats.wordprocessingml.document"})'],
            capture_output=True, text=True, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    if platform.system() == "Linux" and shutil.which("zenity"):
        result = subprocess.run(["zenity", "--file-selection", "--file-filter=*.docx"],
                                capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    try:
        import tkinter
        from tkinter.filedialog import askopenfilename
        window = tkinter.Tk()
        window.withdraw()
        try:
            return askopenfilename(filetypes=[("Word template", "*.docx")]) or None
        finally:
            window.destroy()
    except (ImportError, RuntimeError):
        return None
    except Exception:
        # Headless/locked hosts must remain incomplete, not invent a template.
        return None


def onboard():
    if not sys.stdin.isatty():
        raise ValueError("Open the launcher in a local interactive terminal.")
    dependencies = diagnose()["dependencies"]
    if (not python_path().is_file() or not all(dependencies.values())) and approve(
            "Install private project dependencies and MCP support?"):
        setup(with_mcp=True)
    # Continue with the installed interpreter, not the bootstrap interpreter.
    if python_path().is_file() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        return subprocess.call([str(python_path()), "-B", str(ROOT / "reborn.py"), "onboard"])
    if not shutil.which("ffprobe") and approve("Install FFmpeg/ffprobe using the system package manager?"):
        install_system_tools()
    if not TEMPLATE.is_file():
        print("Choose your authorized existing song-list Word template.")
        if approve("Open a file picker to import it (existing files are never overwritten)?"):
            source = choose_template()
            if source:
                emit(import_template(source))
    if not DATABASE.exists() and approve("Start an empty song library (no historical songs will be invented)?"):
        emit(init_library())
    result = diagnose(online=True)
    if result["pco"]["status"] == "auth_error" and approve("Configure PCO credentials in the local OS credential store?"):
        configure_auth()
        result = diagnose(online=True)
    emit(result)
    if result["mcp_available"]:
        print("Agent connection settings (import only into a host that supports local stdio MCP):")
        emit({"mcpServers": {"reborn": {"command": str(python_path() if python_path().is_file() else Path(sys.executable)),
                                      "args": ["-B", str(ROOT / "reborn.py"), "mcp"]}}})
    else:
        print("MCP SDK is missing. The Agent must use local command execution, or authorize MCP installation.")
    return 0 if result["local_prerequisites_ready"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    install = subs.add_parser("setup")
    install.add_argument("--yes", action="store_true", help="Authorize local dependency installation")
    install.add_argument("--with-mcp", action="store_true")
    install.add_argument("--system-tools", action="store_true", help="Also authorize package-manager FFmpeg installation")
    doctor = subs.add_parser("doctor")
    doctor.add_argument("--online", action="store_true")
    subs.add_parser("auth")
    subs.add_parser("onboard")
    library = subs.add_parser("init-library")
    library.add_argument("--yes", action="store_true")
    template = subs.add_parser("import-template")
    template.add_argument("source", type=Path)
    template.add_argument("--yes", action="store_true")
    run = subs.add_parser("run")
    run.add_argument("engine", choices=("workflow", "mini", "rag"))
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    subs.add_parser("mcp")
    subs.add_parser("mcp-config")
    args = parser.parse_args()
    try:
        if args.command in {"setup", "init-library", "import-template"} and not args.yes:
            emit({"error": "authorization_required", "message": "Review the action and explicitly authorize with --yes."})
            return 2
        if args.command == "setup":
            setup(args.with_mcp, args.system_tools)
        elif args.command == "doctor":
            result = diagnose(args.online)
            emit(result)
            return 0 if result["local_prerequisites_ready"] else 2
        elif args.command == "auth":
            configure_auth()
        elif args.command == "onboard":
            return onboard()
        elif args.command == "init-library":
            emit(init_library())
        elif args.command == "import-template":
            emit(import_template(args.source))
        elif args.command == "run":
            script = {"workflow": "workflow/workflow.py", "mini": "mini-workflow/mini.py",
                      "rag": "rag/rag.py"}[args.engine]
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            return subprocess.call([sys.executable, "-B", str(ROOT / script), *arguments])
        elif args.command == "mcp-config":
            emit({"mcpServers": {"reborn": {
                "command": str(python_path() if python_path().is_file() else Path(sys.executable)),
                "args": ["-B", str(ROOT / "reborn.py"), "mcp"],
            }}})
        else:
            from portable.server import serve
            serve()
        return 0
    except ValueError as error:
        emit({"error": "invalid_setup", "message": str(error)})
        return 2
    except (OSError, RuntimeError, subprocess.SubprocessError, ImportError):
        emit({"error": "setup_or_runtime_failed",
              "message": "Operation failed. Existing materials are preserved; check tools, network and OS permissions. No secret values are logged."})
        return 2

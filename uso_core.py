"""Uso do plano do Claude (sessão de 5h e semanal), lido pela linha de status do Claude Code.

A linha de status é um recurso oficial do Claude Code: a cada atualização ele roda um
comando e entrega a ele um JSON com `rate_limits`. O ajudante `folego_status.py` salva
esse JSON em %APPDATA%/Folego/uso.json, e o widget só lê esse arquivo. O Fôlego nunca
toca no login nem em tokens do Claude.
"""
import ctypes
import datetime as dt
import json
import os
import shutil
import sys
import time
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Folego"
USO = APP_DIR / "uso.json"
ANTERIOR = APP_DIR / "statusline-anterior.json"   # linha de status que o usuário já tinha
SETTINGS = Path.home() / ".claude" / "settings.json"
MARCA = "folego"                                  # identifica o nosso comando no settings.json


class SemDados(Exception):
    """O Claude Code ainda não mandou nenhum dado de uso."""


def _quando(epoch):
    if not epoch:
        return None
    return dt.datetime.fromtimestamp(float(epoch)).astimezone()


def ler_uso():
    """Retorna ({janela: {"pct", "reset"}}, momento_da_leitura). Lança SemDados."""
    try:
        bruto = json.loads(USO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SemDados from None
    agora = time.time()
    janelas = {}
    for nome in ("five_hour", "seven_day"):
        j = (bruto.get("rate_limits") or {}).get(nome) or {}
        reset = j.get("resets_at")
        if reset and float(reset) <= agora:          # a janela já resetou desde a última leitura
            janelas[nome] = {"pct": 0.0, "reset": None}
        else:
            janelas[nome] = {"pct": float(j.get("used_percentage") or 0), "reset": _quando(reset)}
    return janelas, float(bruto.get("updated") or 0)


# ---------------------------------------------------------------- consulta automática

# Pedido mínimo ao próprio Claude Code (modelo leve, prompt curto, sem MCPs e sem gravar
# sessão). O Claude Code responde com um evento rate_limit_event que traz a porcentagem de
# cada janela — inclusive do uso feito pelo app desktop. Custa uma fração ínfima do limite.
ARGS_CONSULTA = ["-p", "ok", "--model", "haiku", "--output-format", "stream-json", "--verbose",
                 "--system-prompt", "Responda ok.", "--strict-mcp-config", "--disable-slash-commands",
                 "--tools", "Read", "--no-session-persistence"]


def mesclar(novo, arquivo):
    """Junta com o que já está salvo: na mesma janela (mesmo reset) o uso só sobe, então um
    dado velho de uma sessão parada nunca sobrescreve um mais novo."""
    try:
        antigo = json.loads(Path(arquivo).read_text(encoding="utf-8")).get("rate_limits") or {}
    except (OSError, ValueError):
        return novo
    out = dict(antigo)
    for nome, j in novo.items():
        a = antigo.get(nome)
        if a and a.get("resets_at") == j.get("resets_at"):
            j = dict(j, used_percentage=max(float(a.get("used_percentage") or 0), float(j.get("used_percentage") or 0)))
        elif a and (a.get("resets_at") or 0) > (j.get("resets_at") or 0):
            j = a                                   # o salvo já é de uma janela mais nova
        out[nome] = j
    return out


class SemClaude(Exception):
    """Claude Code não encontrado neste computador."""


def achar_claude():
    achado = shutil.which("claude")
    if achado:
        return achado
    base = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code"   # o que vem com o app desktop
    exes = sorted(base.glob("*/*/claude.exe"), key=lambda p: p.stat().st_mtime) if base.exists() else []
    if exes:
        return str(exes[-1])
    raise SemClaude


def consultar_claude(timeout=120):
    """Roda o Claude Code escondido e grava o uso em uso.json. Retorna True se conseguiu."""
    import subprocess
    APP_DIR.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([achar_claude()] + ARGS_CONSULTA, cwd=str(APP_DIR), capture_output=True,
                       timeout=timeout, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for linha in r.stdout.decode("utf-8", "replace").splitlines():
        if '"rate_limit_event"' not in linha:
            continue
        try:
            janelas = json.loads(linha)["rate_limit_info"]["unifiedWindows"]
        except (ValueError, KeyError, TypeError):
            continue
        rl = {}
        for nome in ("five_hour", "seven_day"):
            j = janelas.get(nome)
            if j and j.get("utilization") is not None:
                rl[nome] = {"used_percentage": round(float(j["utilization"]) * 100, 1),
                            "resets_at": j.get("resetsAt")}
        if rl:
            rl = mesclar(rl, USO)
            tmp = USO.with_name(f"uso.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"updated": time.time(), "rate_limits": rl, "fonte": "auto"}),
                           encoding="utf-8")
            os.replace(tmp, USO)
            return True
    return False


# ---------------------------------------------------------------- linha de status


def _curto(p):
    """Caminho sem espaços (formato 8.3) e com barras normais, que funciona no Git Bash e no PowerShell."""
    p = str(p)
    if " " in p and os.name == "nt":
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(p, buf, 1024):
            p = buf.value
    return p.replace("\\", "/")


def _comando():
    if getattr(sys, "frozen", False):                 # rodando como Folego.exe
        origem = Path(getattr(sys, "_MEIPASS", "")) / "folego-status.exe"
        destino = APP_DIR / "folego-status.exe"
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if origem.exists() and (not destino.exists() or destino.stat().st_size != origem.stat().st_size):
            shutil.copy2(origem, destino)
        return _curto(destino)
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe":
        py = py.with_name("python.exe")               # a linha de status precisa de saída no console
    return f"{_curto(py)} {_curto(Path(__file__).resolve().with_name('folego_status.py'))}"


def _ler_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _gravar_settings(s):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_name(SETTINGS.name + ".folego.tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS)


def conectado():
    linha = _ler_settings().get("statusLine") or {}
    return MARCA in str(linha.get("command", "")).lower()


def conectar():
    """Configura o Fôlego como linha de status, guardando a que já existia para repassar a ela."""
    s = _ler_settings()
    atual = s.get("statusLine")
    if atual and MARCA not in str(atual.get("command", "")).lower():
        APP_DIR.mkdir(parents=True, exist_ok=True)
        ANTERIOR.write_text(json.dumps(atual, ensure_ascii=False), encoding="utf-8")
    nova = {"type": "command", "command": _comando()}
    if atual and atual.get("padding") is not None:
        nova["padding"] = atual["padding"]
    s["statusLine"] = nova
    _gravar_settings(s)


def atualizar_comando():
    """Mantém o caminho do comando certo se o app mudou de lugar ou de versão."""
    if conectado():
        s = _ler_settings()
        cmd = _comando()
        if s["statusLine"].get("command") != cmd:
            s["statusLine"]["command"] = cmd
            _gravar_settings(s)


def desconectar():
    """Devolve a linha de status anterior (ou remove a nossa)."""
    s = _ler_settings()
    if ANTERIOR.exists():
        s["statusLine"] = json.loads(ANTERIOR.read_text(encoding="utf-8"))
        ANTERIOR.unlink()
    else:
        s.pop("statusLine", None)
    _gravar_settings(s)


if __name__ == "__main__":
    print("conectado:", conectado())
    try:
        print(ler_uso())
    except SemDados:
        print("sem dados ainda")

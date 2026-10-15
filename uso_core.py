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

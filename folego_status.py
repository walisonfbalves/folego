"""Ajudante de linha de status do Fôlego.

O Claude Code roda este comando a cada atualização da linha de status e entrega, pela
entrada padrão, um JSON com `rate_limits` (uso da sessão de 5h e da semana). Aqui:
1. salvamos esses números em %APPDATA%/Folego/uso.json para o widget ler;
2. se o usuário já tinha uma linha de status, repassamos o mesmo JSON para ela e
   devolvemos o que ela imprimir — nada do que ele tinha se perde;
3. senão, imprimimos um resumo curto.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Folego"
USO = APP_DIR / "uso.json"
ANTERIOR = APP_DIR / "statusline-anterior.json"


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


def salvar(rate_limits):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    rate_limits = mesclar(rate_limits, USO)
    tmp = USO.with_name(f"uso.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"updated": time.time(), "rate_limits": rate_limits}), encoding="utf-8")
    os.replace(tmp, USO)


def rodar_anterior(cmd, entrada):
    """Roda a linha de status antiga no mesmo shell que o Claude Code usaria."""
    bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or r"C:\Program Files\Git\bin\bash.exe"
    if os.name != "nt":
        args = ["sh", "-c", cmd]
    elif os.path.exists(bash):
        args = [bash, "-c", cmd]
    else:
        args = [shutil.which("powershell") or "powershell", "-NoProfile", "-Command", cmd]
    try:
        r = subprocess.run(args, input=entrada, capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout
    except Exception:
        return b""


def resumo(rl):
    partes = []
    for nome, rotulo in (("five_hour", "5h"), ("seven_day", "7d")):
        j = (rl or {}).get(nome)
        if j and j.get("used_percentage") is not None:
            partes.append(f"{rotulo} {float(j['used_percentage']):.0f}%")
    return ("fôlego · " + " · ".join(partes)) if partes else ""


def main():
    entrada = sys.stdin.buffer.read()
    try:
        dados = json.loads(entrada.decode("utf-8") or "{}")
    except ValueError:
        dados = {}
    rl = dados.get("rate_limits")
    if rl:
        try:
            salvar(rl)
        except OSError:
            pass

    if ANTERIOR.exists():
        try:
            cmd = json.loads(ANTERIOR.read_text(encoding="utf-8")).get("command", "")
        except (OSError, ValueError):
            cmd = ""
        if cmd:
            sys.stdout.buffer.write(rodar_anterior(cmd, entrada))
            return
    sys.stdout.buffer.write(resumo(rl).encode("utf-8"))


if __name__ == "__main__":
    main()

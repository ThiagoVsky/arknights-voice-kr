#!/usr/bin/env python3
"""Baixa e extrai os assets de midia do servidor EN do Arknights, agrupados por repo futuro.

O download usa a camada de rede do arkprts (resolucao de versao/dominio e o mapeamento
de nome de bundle para nome de arquivo no servidor). A extracao usa UnityPy, que o
arkprts ja depende, com o decodificador LZ4AK que o proprio arkprts registra no lugar do
LZHAM nao implementado pelo UnityPy. Nenhum TextAsset e salvo: o texto do jogo
(gamedata, story, lua) ja vive no repositorio principal.

Uso:
    python tools/assets_sync.py --groups all --out assets --state .state
    python tools/assets_sync.py --groups voice-en --limit 5 --out assets --state .state
    python tools/assets_sync.py --verify --out assets
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import pathlib
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

import aiohttp

from arkprts import network as netn
from arkprts.assets.bundle import (
    asset_path_to_server_filename,
    decompress_lz4ak,
    unzip_only_file,
)
from UnityPy.enums.BundleFile import CompressionFlags
from UnityPy.helpers import CompressionHelper

# obrigatorio antes de qualquer UnityPy.load: o UnityPy nao implementa LZHAM
CompressionHelper.DECOMPRESSION_MAP[CompressionFlags.LZHAM] = decompress_lz4ak

SERVER = "en"
PLATFORM = "Android"

# Cada repositorio de assets recebe uma copia deste script com o seu grupo como padrao,
# para funcionar sozinho sem precisar passar --groups. O valor e trocado na copia, e
# pode ser sobreposto pela variavel de ambiente ASSETS_GROUP.
DEFAULT_GROUP = os.environ.get("ASSETS_GROUP", "voice-kr")

# ---------------------------------------------------------------------------
# Grupos. Cada chave vira uma pasta em <out>/ e, depois, um repositorio proprio.
#   match    : prefixos de nome de bundle que pertencem ao grupo
#   exclude  : prefixos descartados do grupo
#   strip    : prefixos removidos do caminho de container, na ordem
# ---------------------------------------------------------------------------
VOICE_PREFIXES = (
    "audio/sound_beta_2/voice/",
    "audio/sound_beta_2/voice_cn/",
    "audio/sound_beta_2/voice_en/",
    "audio/sound_beta_2/voice_kr/",
    "audio/sound_beta_2/voice_custom/",
)

GROUPS: dict[str, dict[str, Any]] = {
    # voz por idioma. Em EN, 'voice/' sem sufixo e o conjunto japones.
    "voice-jp": {"match": ("audio/sound_beta_2/voice/",), "strip": ("dyn/audio/sound_beta_2/voice/",)},
    "voice-cn": {"match": ("audio/sound_beta_2/voice_cn/",), "strip": ("dyn/audio/sound_beta_2/voice_cn/",)},
    "voice-en": {"match": ("audio/sound_beta_2/voice_en/",), "strip": ("dyn/audio/sound_beta_2/voice_en/",)},
    "voice-kr": {"match": ("audio/sound_beta_2/voice_kr/",), "strip": ("dyn/audio/sound_beta_2/voice_kr/",)},
    "voice-custom": {"match": ("audio/sound_beta_2/voice_custom/",), "strip": ("dyn/audio/sound_beta_2/voice_custom/",)},
    # som que nao e voz: musica, efeitos, ambiencia, sons de inimigo e do jogador
    "sound": {
        "match": ("audio/",),
        "exclude": VOICE_PREFIXES,
        "strip": ("dyn/audio/sound_beta_2/", "dyn/audio/custom_se/", "dyn/audio/"),
    },
    # todo o resto dos dados do servidor EN, incluindo o que nao e extraivel
    # (meshes, prefabs): nesses casos o .ab cru e preservado.
    "en": {"exclude": ("audio/",), "strip": ("dyn/",)},
}

LOCK = threading.Lock()
LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    with LOG_LOCK:
        print(line, flush=True)


def strip_prefix(path: str, prefixes: Iterable[str]) -> str:
    for p in prefixes:
        if path.startswith(p):
            return path[len(p):]
    return path


def select(group: str, ab_infos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spec = GROUPS[group]
    match = spec.get("match")
    exclude = spec.get("exclude", ())
    out = []
    for info in ab_infos:
        name = info["name"]
        if any(name.startswith(p) for p in exclude):
            continue
        if match and not any(name.startswith(p) for p in match):
            continue
        out.append(info)
    return out


class State:
    """Estado por grupo: nome de bundle -> hash processado."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"resVersion": None, "done": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                log(f"estado ilegivel em {path}, recomecando")
        self.data.setdefault("done", {})

    def is_done(self, name: str, digest: str) -> bool:
        with LOCK:
            return self.data["done"].get(name) == digest

    def mark(self, name: str, digest: str) -> None:
        with LOCK:
            self.data["done"][name] = digest
            self._flush()

    def reset(self) -> None:
        with LOCK:
            self.data["done"] = {}
            self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)


AUDIO_MAGIC = {
    b"OggS": ".ogg",
    b"RIFF": ".wav",
    b"FSB5": ".fsb",
    b"\xff\xfb": ".mp3",
    b"ID3": ".mp3",
}


def encode_audio_payload(name: str, blob: bytes, mode: str) -> tuple[str, bytes]:
    """Decide a extensao e o conteudo de um clipe de audio.

    O UnityPy entrega o audio ja decodificado (WAV PCM) para os bancos FSB5. WAV
    cru e cerca de 10x maior que a origem, o que estoura o limite de repositorio do
    GitHub, entao o padrao e reencodar para MP3. 'original' preserva o que o UnityPy
    devolveu, sem reencode.
    """
    if blob[:4] == b"RIFF" and mode == "mp3":
        try:
            import lameenc
            import wave

            with wave.open(io.BytesIO(blob), "rb") as wav:
                channels = wav.getnchannels()
                rate = wav.getframerate()
                width = wav.getsampwidth()
                frames = wav.readframes(wav.getnframes())
            if width != 2:
                return f"{name}.wav", blob
            encoder = lameenc.Encoder()
            # 96 kbps mono e suficiente para voz; musica costuma ser estereo e pede mais
            encoder.set_bit_rate(96 if channels == 1 else 160)
            encoder.set_in_sample_rate(rate)
            encoder.set_channels(channels)
            encoder.set_quality(2)
            payload = encoder.encode(frames) + encoder.flush()
            return f"{name}.mp3", bytes(payload)
        except Exception as exc:
            log(f"      mp3 falhou para {name}, mantendo wav: {type(exc).__name__}: {exc}")
            return f"{name}.wav", blob
    ext = AUDIO_MAGIC.get(blob[:4])
    if ext:
        return f"{name}{ext}", blob
    return f"{name}.bytes", blob


def object_paths(obj: Any) -> list[str]:
    """Caminhos de container de um objeto.

    Em UnityPy 1.25 `obj.container` e uma string (e nao uma lista), entao iterar
    direto sobre ele produz uma lista de caracteres. Trata os dois casos.
    """
    try:
        container = obj.container
    except Exception:
        return []
    if not container:
        return []
    if isinstance(container, str):
        return [container]
    return [str(p) for p in container]


def extract_bundle(raw: bytes, spec: dict[str, Any], written: list[pathlib.Path]) -> int:
    """Extrai midia de um .ab. Retorna quantos arquivos foram gravados.

    A pasta de destino vem do caminho de container do bundle (que espelha a arvore
    `dyn/...` do jogo) e o nome do arquivo vem de m_Name. Sem container, o nome
    cru e usado. TextAsset nunca e gravado: o texto do jogo ja esta no repositorio
    principal.
    """
    import UnityPy

    out_dir: pathlib.Path = spec["out_dir"]
    strip: tuple[str, ...] = spec["strip"]
    mode: str = spec.get("audio_format", "mp3")
    saved = 0
    written_names: set[str] = set()

    env = UnityPy.load(io.BytesIO(raw))
    for obj in env.objects:
        kind = obj.type.name
        try:
            paths = object_paths(obj)
            container = paths[0] if paths else ""

            if kind in ("Texture2D", "Sprite"):
                data = obj.read()
                name = str(data.m_Name)
                folder = os.path.dirname(container)
                rel = f"{folder}/{name}" if folder else name
                rel = strip_prefix(rel, strip)
                if rel in written_names:
                    continue
                written_names.add(rel)
                target = out_dir / f"{rel}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                data.image.save(target)
                written.append(target)
                saved += 1
            elif kind == "AudioClip":
                data = obj.read()
                if container:
                    base = strip_prefix(os.path.splitext(container)[0], strip)
                else:
                    base = strip_prefix(str(data.m_Name), strip)
                # m_AudioData costuma ser None: os bancos FSB5 vivem no .resource do
                # bundle e o UnityPy os decodifica em `samples` ({nome: bytes}).
                for sample_name, blob in (data.samples or {}).items():
                    if not blob:
                        continue
                    if "." in os.path.basename(base):
                        base = os.path.splitext(base)[0]
                    out_name, payload = encode_audio_payload(base, bytes(blob), mode)
                    if out_name in written_names:
                        continue
                    written_names.add(out_name)
                    target = out_dir / out_name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                    written.append(target)
                    saved += 1
            elif kind == "TextAsset":
                continue
        except Exception as exc:
            log(f"      falha em {kind}: {type(exc).__name__}: {exc}")
            continue

    return saved


async def run(args: argparse.Namespace) -> int:
    out_root = pathlib.Path(args.out)
    state_dir = pathlib.Path(args.state)
    state_dir.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)

    groups = list(GROUPS) if args.groups == "all" else [g.strip() for g in args.groups.split(",")]
    for g in groups:
        if g not in GROUPS:
            log(f"grupo desconhecido: {g}")
            return 2

    log(f"resolvendo configuracao do servidor {SERVER}/{PLATFORM}")
    session = netn.NetworkSession(default_server=SERVER)
    await session.load_version_config(SERVER, PLATFORM)
    hu = session.domains[SERVER]["hu"]
    res_version = session.versions[(SERVER, PLATFORM)]["resVersion"]
    base_url = f"{hu}/{PLATFORM}/assets/{res_version}/"
    log(f"resVersion {res_version}")
    log(f"base {base_url}")

    hot_path = state_dir / f"hot_update_list_{SERVER}.json"
    if hot_path.exists() and not args.refresh_manifest:
        ab_infos = json.loads(hot_path.read_text(encoding="utf-8"))["abInfos"]
        log(f"manifesto do cache local: {len(ab_infos)} bundles")
    else:
        async with aiohttp.ClientSession() as http:
            async with http.get(base_url + "hot_update_list.json") as r:
                r.raise_for_status()
                manifest = await r.json(content_type=None)
        hot_path.write_text(json.dumps(manifest), encoding="utf-8")
        ab_infos = manifest["abInfos"]
        log(f"manifesto baixado: {len(ab_infos)} bundles")

    total_start = time.time()
    summary: list[tuple[str, int, int, float]] = []

    for group in groups:
        entries = select(group, ab_infos)
        state = State(state_dir / f"{group}.json")
        state.data["resVersion"] = res_version
        if args.force:
            state.reset()

        pending = [e for e in entries if not state.is_done(e["name"], e["hash"])]
        if args.limit:
            pending = pending[: args.limit]

        decl = sum(e.get("totalSize", 0) for e in pending)
        log(
            f"grupo {group}: {len(entries)} bundles no manifesto, "
            f"{len(entries) - len(pending)} ja processados, {len(pending)} pendentes "
            f"({decl / 1024**3:.2f} GiB declarados)"
        )
        if not pending:
            summary.append((group, 0, 0, 0.0))
            continue

        out_dir = out_root if args.flat else out_root / group
        out_dir.mkdir(parents=True, exist_ok=True)
        spec = {
            "out_dir": out_dir,
            "strip": tuple(GROUPS[group].get("strip", ())),
            "audio_format": args.audio_format,
        }

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=args.concurrency * 3)
        counters = {"bundles": 0, "files": 0, "bytes": 0, "failed": 0}
        executor = ThreadPoolExecutor(max_workers=args.concurrency)

        def process(info: dict[str, Any], blob: bytes) -> None:
            name = info["name"]
            try:
                raw = unzip_only_file(blob)
                counters["bytes"] += len(raw)
                written: list[pathlib.Path] = []
                if raw[:8].startswith(b"Unity") or raw[:4] == b"UnityFS":
                    n = extract_bundle(raw, spec, written)
                    if n == 0:
                        # nada extraivel (meshes, prefabs, shaders): preserva o bundle
                        # cru para nao perder o dado
                        rel = os.path.splitext(strip_prefix(name, spec["strip"]))[0]
                        target = out_dir / f"{rel}.ab"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(raw)
                        written.append(target)
                else:
                    target = out_dir / strip_prefix(name, spec["strip"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    written.append(target)
                counters["files"] += len(written)
                state.mark(name, info["hash"])
            except Exception as exc:
                counters["failed"] += 1
                log(f"   FALHA {name}: {type(exc).__name__}: {exc}")
            finally:
                counters["bundles"] += 1
                n = counters["bundles"]
                if n % args.log_every == 0 or n == len(pending):
                    elapsed = time.time() - total_start
                    log(
                        f"   {group}: {n}/{len(pending)} bundles, "
                        f"{counters['files']} arquivos, {counters['bytes'] / 1024**3:.2f} GiB extraidos, "
                        f"{counters['failed']} falhas, {elapsed / 60:.1f} min"
                    )

        async def worker(http: aiohttp.ClientSession) -> None:
            while True:
                info = await queue.get()
                if info is None:
                    # o sentinela tambem conta como item da fila: sem o task_done()
                    # aqui, o queue.join() abaixo nunca retorna.
                    queue.task_done()
                    break
                url = base_url + asset_path_to_server_filename(info["name"])
                blob = None
                for attempt in range(args.retries):
                    try:
                        async with http.get(url) as response:
                            response.raise_for_status()
                            blob = await response.read()
                        break
                    except Exception as exc:
                        if attempt + 1 == args.retries:
                            counters["failed"] += 1
                            log(f"   download falhou {info['name']}: {type(exc).__name__}: {exc}")
                        else:
                            await asyncio.sleep(2 * (attempt + 1))
                if blob:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(executor, process, info, blob)
                queue.task_done()

        started = time.time()
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=args.concurrency * 2),
            timeout=aiohttp.ClientTimeout(total=args.timeout),
        ) as http:
            workers = [asyncio.create_task(worker(http)) for _ in range(args.concurrency)]
            for info in pending:
                await queue.put(info)
            for _ in workers:
                await queue.put(None)
            await queue.join()
            for w in workers:
                await w
        executor.shutdown(wait=True)

        size = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
        summary.append((group, counters["bundles"], size, time.time() - started))
        log(
            f"grupo {group} concluido: {counters['files']} arquivos, "
            f"{size / 1024**3:.3f} GiB em disco, {counters['failed']} falhas, "
            f"{(time.time() - started) / 60:.1f} min"
        )

    await session.close()

    log("")
    log("resumo:")
    for group, bundles, size, elapsed in summary:
        log(f"   {group:14s} {bundles:6d} bundles  {size / 1024**3:8.3f} GiB  {elapsed / 60:7.1f} min")
    return 0


def verify(out_root: pathlib.Path, flat: bool = False) -> int:
    """Relatorio de tamanho e contagem por grupo e por extensao."""
    if not out_root.exists():
        print(f"{out_root} nao existe")
        return 1
    SKIP = {".git", ".state", ".github", "tools", "scripts", "node_modules", ".venv-assets"}
    if flat:
        # repositorio dedicado: o payload esta na propria raiz
        units = [out_root]
    else:
        units = sorted(p for p in out_root.iterdir() if p.is_dir() and p.name not in SKIP)
    grand = 0
    for group_dir in units:
        files = [
            f
            for f in group_dir.rglob("*")
            if f.is_file() and not (SKIP & set(f.relative_to(group_dir).parts))
        ]
        size = sum(f.stat().st_size for f in files)
        grand += size
        exts: dict[str, list[int]] = {}
        for f in files:
            e = f.suffix.lower() or "(sem extensao)"
            agg = exts.setdefault(e, [0, 0])
            agg[0] += 1
            agg[1] += f.stat().st_size
        print(f"{group_dir.name:14s} {len(files):7d} arquivos  {size / 1024**3:8.3f} GiB")
        for e, (n, s) in sorted(exts.items(), key=lambda kv: -kv[1][1])[:6]:
            print(f"                 {e:12s} {n:7d}  {s / 1024**3:8.3f} GiB")
        top = sorted((f for f in files), key=lambda f: -f.stat().st_size)[:5]
        for f in top:
            print(f"                 maior: {f.relative_to(group_dir)}  {f.stat().st_size / 1024**2:.1f} MiB")
    print(f"{'TOTAL':14s} {'':7s}           {grand / 1024**3:8.3f} GiB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--groups", default=DEFAULT_GROUP)
    parser.add_argument("--out", default="assets")
    parser.add_argument("--state", default=".state")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--limit", type=int, default=0, help="processa no maximo N bundles por grupo")
    parser.add_argument(
        "--audio-format",
        choices=("mp3", "original"),
        default="mp3",
        help="mp3 reencoda o WAV decodificado (padrao, ~10x menor); original mantem o que o UnityPy devolveu",
    )
    parser.add_argument("--log-every", type=int, default=25)
    parser.add_argument(
        "--flat",
        action="store_true",
        help="escreve o conteudo do grupo direto em --out, sem a subpasta com o nome do grupo "
        "(usado nos repositorios dedicados, onde o payload fica na raiz)",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--refresh-manifest", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if args.verify:
        return verify(pathlib.Path(args.out), flat=args.flat)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log("interrompido")
        return 130
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

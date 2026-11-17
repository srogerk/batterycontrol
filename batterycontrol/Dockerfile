ARG BUILD_FROM
FROM ${BUILD_FROM}

# Kallkoden hamtas fran GitHub vid build istallet for att kopieras in i
# batterycontrol/. HA bygger bara med batterycontrol/ som kontext, sa src/
# i reporoten nas inte darifran - och att duplicera koden hit skulle skapa
# tva sanningar som glider isar (exakt felet med .startup.sh).
#
# Pinnas till en ref: en gren, en tagg eller ett fullstandigt commit-SHA.
# main = senaste. En tagg eller ett SHA ger en reproducerbar release.
#
# OBS: anvand INTE "git clone --branch" for att pinna. --branch tar bara gren-
# och taggnamn, aldrig ett SHA. En tidigare variant hade
#
#     git clone --depth 1 --branch "${REPO_REF}" ... || git clone "${REPO_URL}" ...
#
# dar fallbacken tyst klonade default-grenens topp nar REPO_REF var ett SHA.
# Builden blev gron men inneholl fel kod. git fetch/checkout nedan tar alla tre
# ref-typerna och failar hogt pa en ref som inte finns.
ARG REPO_URL=https://github.com/srogerk/batterycontrol.git
ARG REPO_REF=main

# Supervisor skickar alltid med versionen som build-arg (se byggkommandot i
# Supervisors logg: --build-arg BUILD_VERSION=0.6.5). Vi MASte rora den i
# steget som hamtar kallkoden, annars cachar BuildKit det lagret.
#
# Fallan: REPO_REF=main ar samma strang i varje bygge. BuildKit ser identiska
# ARG-varden och ateranvander lagret "git fetch origin main" fran forra bygget.
# Imagen far da koden som main pekade pa NAR CACHEN SKAPADES, inte nu. Bygget
# blir gront, versionen stammer, men koden ar gammal - och ingenting i loggen
# sager det. Det kostade en felsokningsrunda 2026-11-17: 0.6.4 och 0.6.5
# inneholl bada src/ fran 0.6.3, medan config.yaml (som Supervisor laser
# direkt fran repot, utanfor imagen) uppdaterades korrekt. Darfor fungerade
# homeassistant_api-fixen men inte paths-fixen.
#
# BUILD_VERSION i detta RUN ogiltigforklarar lagret vid varje ny version.
# Satt den INTE utanfor RUN - da cachar BuildKit anda, eftersom ett ARG som
# inte anvands i ett steg inte paverkar stegets cache-nyckel.
ARG BUILD_VERSION=dev

# ---------------------------------------------------------------------------
# VARFOR apk OCH INTE pip FOR numpy/scipy/matplotlib
#
# HA-boxen ar en QEMU-VM med CPU-modellen "Common KVM processor". Den saknar
# SSE3/SSSE3/SSE4.1/SSE4.2/POPCNT/AVX. PyPI:s musllinux-hjul for numpy ar
# byggda for x86-64-v2 (kraver SSE4.2 + POPCNT) och vagrar ladda:
#
#   RuntimeError: NumPy was built with baseline optimizations:
#   (X86_V2) but your machine doesn't support: (X86_V2).
#
# Alpine bygger sina py3-numpy/py3-scipy med "-Dcpu-baseline=none
# -Dcpu-dispatch=none" - inga ISA-krav alls. apk-paketen ar alltsa det enda
# som startar pa den har CPU:n. Byt inte tillbaka till pip-hjul utan att
# forst ha verifierat att hjulen klarar en CPU utan SSE4.2.
#
# pandas ar struket: ingen fil i src/ importerar det.
# holidays finns inte i Alpine (kholidays ar KDE:s lib, inte samma sak) och
# ar ren python (py3-none-any) - darfor det enda paketet som gar via pip.
# ---------------------------------------------------------------------------

# Steg 1: systempaket. python3 ur apk ar 3.14; py3-pip ar ett separat paket
# och maste namnas - annars finns ingen pip3 for holidays.
RUN apk add --no-cache \
        git \
        tzdata \
        python3 \
        py3-pip \
        py3-numpy \
        py3-scipy \
        py3-matplotlib \
        py3-yaml

# Steg 2: holidays (ren python, inga binara hjul, ingen ISA-fraga).
RUN pip3 install --no-cache-dir --break-system-packages holidays

# Steg 3: hamta kallkoden. "git fetch <ref>" loser gren, tagg OCH commit-SHA;
# "git clone --branch" gor det inte (den vagrar ett SHA, och en ||-fallback
# klonar da tyst default-grenen sa bygget blir gront med fel kod).
RUN echo "hamtar kallkod ${REPO_REF} for version ${BUILD_VERSION}" \
    && mkdir -p /app/src-repo \
    && cd /app/src-repo \
    && git init -q \
    && git remote add origin "${REPO_URL}" \
    && ( git fetch -q --depth 1 origin "${REPO_REF}" \
         || git fetch -q --depth 1 origin "refs/tags/${REPO_REF}" ) \
    && git checkout -q FETCH_HEAD \
    && cd / \
    && mkdir -p /app \
    && cp -r /app/src-repo/src /app/src \
    && cp -r /app/src-repo/web /app/web \
    && git -C /app/src-repo rev-parse --short HEAD > /app/COMMIT \
    && rm -rf /app/src-repo

ENV TZ=Europe/Stockholm \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY rootfs /

RUN chmod +x /app/run.sh

EXPOSE 8099

CMD [ "/app/run.sh" ]

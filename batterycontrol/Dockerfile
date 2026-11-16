ARG BUILD_FROM
FROM ${BUILD_FROM}

# Källkoden hämtas från GitHub vid build istället för att kopieras in i
# batterycontrol/. HA bygger bara med batterycontrol/ som kontext, så src/
# i reporoten nås inte därifrån — och att duplicera koden hit skulle skapa
# två sanningar som glider isär (exakt felet med .startup.sh).
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

RUN apk add --no-cache \
        git \
        python3 \
        py3-pip \
        py3-numpy \
        tzdata \
    && pip3 install --no-cache-dir --break-system-packages \
        scipy \
        matplotlib \
        holidays \
        pandas \
        pyyaml \
    # Beroendena ligger i imagen — inte i en .startup.sh som kan glömma ett paket.
    # Hamta exakt den ref som REPO_REF pekar pa - gren, tagg eller commit-SHA.
    #
    # "git fetch <ref>" loser alla tre; "git clone --branch" gor det inte (den
    # vagrar ett SHA, och en ||-fallback klonar da tyst default-grenen).
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
    && rm -rf /app/src-repo

ENV TZ=Europe/Stockholm \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY rootfs /

RUN chmod +x /app/run.sh

EXPOSE 8099

CMD [ "/app/run.sh" ]

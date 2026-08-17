#!/bin/sh
# Make a fresh deployment work with nothing mounted and nothing prepared.
#
# On a new host /data is an empty volume, so the first container to start
# writes a starter config there. Both services run this, so whichever wins the
# race creates it and the other simply finds it already present.
set -e

: "${GLEICHNASS_CONFIG:=/data/gleichnass.yaml}"

if [ ! -f "$GLEICHNASS_CONFIG" ]; then
    echo "gleichnass: no config at $GLEICHNASS_CONFIG, writing a starter one"
    gleichnass init -c "$GLEICHNASS_CONFIG" || true
fi

if [ ! -f "$GLEICHNASS_CONFIG" ]; then
    echo "gleichnass: could not create $GLEICHNASS_CONFIG; is /data writable?" >&2
    exit 1
fi

exec "$@"

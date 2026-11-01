#!/usr/bin/env sh
# Heroku dyno boot hook: ensure pulseaudio and apt private shared libraries are on LD_LIBRARY_PATH
# Fixes: ffprobe: error while loading shared libraries: libpulsecommon-*.so
if [ -d "/app/.apt" ]; then
    for dir in /app/.apt/usr/lib/*/pulseaudio /app/.apt/usr/lib/x86_64-linux-gnu/pulseaudio /app/.apt/usr/lib/pulseaudio /app/.apt/lib/x86_64-linux-gnu/pulseaudio /usr/lib/*/pulseaudio; do
        if [ -d "$dir" ]; then
            case ":${LD_LIBRARY_PATH:-}:" in
                *":$dir:"*) ;;
                *) export LD_LIBRARY_PATH="$dir:${LD_LIBRARY_PATH:-}" ;;
            esac
        fi
    done
fi

#!/bin/sh
exec journalctl --user -u music-assistant-mp3-backfill.service -f

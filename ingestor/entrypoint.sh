#!/bin/sh
# Starts Nginx (serving --nginx_webroot) in the background, then runs the
# ingestor in the foreground so it receives docker stop's SIGTERM.
set -e

webroot=/var/www/html
prev=""
for arg in "$@"; do
    case "$prev" in --nginx_webroot) webroot="$arg" ;; esac
    case "$arg" in --nginx_webroot=*) webroot="${arg#--nginx_webroot=}" ;; esac
    prev="$arg"
done

if [ "${LP_DISABLE_NGINX:-0}" != "1" ]; then
    mkdir -p "$webroot"
    sed -i "s#^\(\s*root\) .*;#\1 $webroot;#" /etc/nginx/conf.d/livepulse.conf
    nginx
fi

exec python /app/lp_ingestor.py "$@"

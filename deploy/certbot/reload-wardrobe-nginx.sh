#!/bin/sh
set -eu

case " ${RENEWED_DOMAINS:-} " in
    *" wardrobe.example.com "*)
        /usr/sbin/nginx -t
        /usr/bin/systemctl reload nginx
        ;;
esac

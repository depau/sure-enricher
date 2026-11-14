# Chromium for interactive provider sign-ins. Users see and drive it from /enrich (CDP screencast + input);
# each sign-in runs in its own browser context, disposed after the session is imported. Keep CDP internal.
FROM alpine:3
RUN apk add --no-cache chromium xvfb socat font-noto ttf-dejavu && adduser -D -u 1000 b
COPY start.sh /start.sh
USER b
EXPOSE 9222
ENTRYPOINT ["sh", "/start.sh"]

FROM nginx:alpine

RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.bfsu.edu.cn/g' /etc/apk/repositories &&\
    apk update && apk add bash curl

COPY nginx.conf /etc/nginx/nginx.conf

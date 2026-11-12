FROM postgres:16-alpine
COPY init-databases.sh /docker-entrypoint-initdb.d/10-init-databases.sh
RUN chmod 755 /docker-entrypoint-initdb.d/10-init-databases.sh

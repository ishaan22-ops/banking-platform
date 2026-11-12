FROM postgres:16-alpine
COPY init-databases.sh /scripts/init-databases.sh
COPY init-databases.sh /docker-entrypoint-initdb.d/10-init-databases.sh
RUN sed -i 's/\r$//' /scripts/init-databases.sh /docker-entrypoint-initdb.d/10-init-databases.sh \
    && chmod 755 /scripts/init-databases.sh /docker-entrypoint-initdb.d/10-init-databases.sh

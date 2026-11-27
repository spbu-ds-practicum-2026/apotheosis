# PostgreSQL 16 под управлением Patroni (DCS — etcd v3).
FROM postgres:16

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv curl \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/patroni \
 && /opt/patroni/bin/pip install --no-cache-dir "patroni[etcd3]==4.0.4" "psycopg2-binary==2.9.10" \
 && ln -s /opt/patroni/bin/patroni /usr/local/bin/patroni \
 && ln -s /opt/patroni/bin/patronictl /usr/local/bin/patronictl \
 && mkdir -p /var/lib/postgresql/data /var/run/postgresql \
 && chown -R postgres:postgres /var/lib/postgresql /var/run/postgresql

COPY patroni.yml /etc/patroni.yml
COPY post_bootstrap.sh /usr/local/bin/post_bootstrap.sh
RUN chmod 755 /usr/local/bin/post_bootstrap.sh

USER postgres
EXPOSE 5432 8008
ENTRYPOINT []
CMD ["patroni", "/etc/patroni.yml"]

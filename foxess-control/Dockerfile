ARG BUILD_FROM
FROM ${BUILD_FROM}

COPY foxess-control/run.sh /
RUN chmod a+x /run.sh

COPY custom_components/foxess_control/ /opt/foxess_control/

CMD [ "/run.sh" ]

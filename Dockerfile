# plain InterSystems IRIS Community (no IRIS for Health / FHIR) + Embedded Python libraries.
# Multi-arch index digest of intersystems/iris-community:2026.1, verified on linux/amd64
ARG IRIS_IMAGE=intersystems/iris-community:2026.1@sha256:c57b65b2b454494091e7b3e49f6a53b3335f40adf475bcfcee0866083f35a7c2
FROM ${IRIS_IMAGE}

USER ${ISC_PACKAGE_MGRUSER}

COPY --chown=${ISC_PACKAGE_MGRUSER}:${ISC_PACKAGE_IRISGROUP} requirements.txt constraints.txt /tmp/hub/
RUN pip3 install --no-cache-dir --disable-pip-version-check \
        --target /usr/irissys/mgr/python \
        -r /tmp/hub/requirements.txt -c /tmp/hub/constraints.txt \
 && PYTHONPATH=/usr/irissys/mgr/python python3 -m pip check --disable-pip-version-check

# application Python package, on the default Embedded Python path
COPY --chown=${ISC_PACKAGE_MGRUSER}:${ISC_PACKAGE_IRISGROUP} src/python/dc_hub /usr/irissys/mgr/python/dc_hub

COPY --chown=${ISC_PACKAGE_MGRUSER}:${ISC_PACKAGE_IRISGROUP} src/cls /tmp/hub/cls
COPY --chown=${ISC_PACKAGE_MGRUSER}:${ISC_PACKAGE_IRISGROUP} iris.script /tmp/hub/iris.script
RUN iris start IRIS \
 && iris session IRIS -U %SYS < /tmp/hub/iris.script \
 && iris stop IRIS quietly

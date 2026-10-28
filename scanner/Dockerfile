FROM owasp/dependency-check:13.0.0@sha256:0e8f69ad5b00d62000937f4e57189812343f6903d9c83ee712012d51542a020a

LABEL org.opencontainers.image.source="https://github.com/Disease-Prediction-ASET26/disease-prediction-ci"
# Keep the immutable seed separate from each scan's writable data.
COPY snapshot/ /opt/odc-snapshot/
USER 1000:1000

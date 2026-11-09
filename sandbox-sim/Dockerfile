# sandbox-sim: deterministic simulation harness for the sandbox-api core.
# The core package is copied from the `app` additional build context
# (compose: additional_contexts: app: ../sandbox-api/api/app) so the
# simulator always tests the exact code the real API ships.
FROM python:3.12-slim
WORKDIR /work
RUN pip install --no-cache-dir pytest==8.3.4
COPY --from=app . /work/app
COPY --from=sched . /work/sched/app
COPY sim/ /work/sim/
COPY tests/ /work/tests/
ENV PYTHONPATH=/work PYTHONDONTWRITEBYTECODE=1 SCHED_APP_DIR=/work/sched/app
CMD ["python", "-m", "sim.run", "--seeds", "300", "--steps", "300"]

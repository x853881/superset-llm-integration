# Running the suite in production

This folder holds everything production needs to run the LLM-quality suite.
The suite runs as a Kubernetes Job in Superset's namespace, from the image
the suite's [Dockerfile](../Dockerfile) builds. It asks the Superset and
plugin people already use, and its judge is a model on the plugin's own
model server. Production has none of the dev test data.

You change two files, both in this folder, and no code:

| File | What you change |
|---|---|
| [settings.env](settings.env) | The six values marked `<...>`, and two lines to check (step 2) |
| [job.yaml](job.yaml) | The image, on its `image:` line (step 5) |

## What you need

- Somewhere to build the image and a registry the cluster pulls from. The
  build installs Python packages from PyPI, or from a mirror given as
  `--build-arg PIP_INDEX=<url>`.
- `kubectl` access to Superset's namespace.
- Network access from the namespace to Superset and to the model server.
- A Superset user for the suite, with the Gamma, sql_lab and EXTENSIONS
  roles, and the ID of a database it can query.
- The headers that tell Superset who is calling, and the values that make
  the caller that user (`SUPERSET_HEADERS`). Production's Superset takes its
  users from headers, not a password, so the suite sends them itself.
- A client certificate and key the model server accepts, as PEM files, and
  the model server's CA file if the plugin uses one. A `.p12` or `.pfx`
  certificate converts with `openssl pkcs12`, as the plugin's README shows.
- The name of a model on that server to judge with, other than the plugin's.

## Steps

Do them in order, from this folder (`llm-eval-suite/production`). Every
`kubectl` command runs in Superset's namespace: add `-n <namespace>` to each,
or make it your context's default.

1. **Build the image and push it.**

   ```bash
   docker build -t <registry>/llm-eval-suite:<tag> \
     --build-arg BASE_IMAGE=<base image with Python 3.11> ..
   docker push <registry>/llm-eval-suite:<tag>
   ```

   The `..` at the end is the suite's folder, which the image is built
   from. `BASE_IMAGE` is any image with Python 3.11, such as
   `registry.access.redhat.com/ubi9/python-311`. Without it, the image builds
   on `python:3.11-slim`. If the build can't reach pypi.org, add
   `--build-arg PIP_INDEX=<mirror address>`.

2. **Fill in the settings and make them a ConfigMap.** In `settings.env`:

   - Replace the value on each of the six lines marked `<...>`:
     `SUPERSET_URL`, `SUPERSET_HEADERS`, `SUPERSET_DATABASE_ID`,
     `SUPERSET_SCHEMA`, `JUDGE_MODEL_NAME` and `JUDGE_BASE_URL`. The comment
     above each line says what goes there and who knows it.
   - Check two lines that already hold a value, against the plugin's
     settings in production's `superset_config_docker.py`. In
     `JUDGE_HEADERS`, the header's name must be the one in the plugin's
     `NLSQL_LLM_USER_HEADER`. `JUDGE_CA_BUNDLE` stays as it is when the
     plugin sets `NLSQL_LLM_CA_BUNDLE`. Delete either line if the plugin
     doesn't set its setting.
   - If a header's value in `SUPERSET_HEADERS` is a secret, such as a token,
     delete that line here: it goes in step 4's Secret.
   - Leave every other line as it is.

   ```bash
   kubectl create configmap llm-eval-suite-settings --from-env-file=settings.env
   ```

   The suite won't start while any `<...>` is left: it names the settings
   that still hold one.

3. **Put the certificate files in a Secret.** They are the suite's client
   certificate, its key, and the model server's CA file (the file the
   plugin's `NLSQL_LLM_CA_BUNDLE` names), all as PEM files.

   ```bash
   kubectl create secret generic llm-eval-suite-certs \
     --from-file=client.pem=<certificate> \
     --from-file=client.key=<key> \
     --from-file=ca.pem=<CA file>
   ```

   Keep the names `client.pem`, `client.key` and `ca.pem`: the settings look
   for those files in `/certs`. Leave out the `ca.pem` line if you deleted
   `JUDGE_CA_BUNDLE` in step 2.

4. **Only if a setting is a secret: make the login Secret.** Skip this step
   otherwise. Three settings can be secrets: the password of an encrypted
   key, a `SUPERSET_HEADERS` line that holds a token, and
   `SUPERSET_PASSWORD`, only for a Superset that signs in with a password.
   Put whichever you have, and nothing else, in a file `login.env` in this
   folder:

   ```
   JUDGE_CLIENT_KEY_PASSWORD=<password>
   SUPERSET_HEADERS=<the line you left out of settings.env>
   ```

   ```bash
   kubectl create secret generic llm-eval-suite-login --from-env-file=login.env
   rm login.env
   ```

   Git ignores `login.env`, and certificate and key files, in case one is
   left here.

5. **Run the Job.** In `job.yaml`, replace `<registry>/llm-eval-suite:<tag>`
   on the `image:` line with the image from step 1. Then:

   ```bash
   kubectl apply -f job.yaml
   kubectl logs -f job/llm-eval-suite
   ```

6. **Read the result.** The Job asks the plugin four questions that need no
   tables and grades the answers. The report is the last thing in the log,
   and its top line is the result:

   ```bash
   kubectl logs job/llm-eval-suite | grep "questions passed"
   ```

   ```
   4 of 4 questions passed   average grade 100%   part: smoke
   ```

   With 4 of 4, Superset, SQL Lab, the plugin, its model and the judge all
   connect, and `kubectl get job llm-eval-suite` shows the Job complete.
   With fewer, the Job is marked failed, and the report gives the error for
   each question that failed.

## If a step fails

The report in the pod's log has the error. The usual causes:

| Error mentions | Likely cause |
|---|---|
| `JUDGE_CLIENT_CERT`, `JUDGE_CLIENT_KEY` or `JUDGE_CA_BUNDLE` | That setting's file is missing, unreadable or not PEM |
| `CERTIFICATE_VERIFY_FAILED` | The server's certificate comes from an in-house CA. For the model server, set `JUDGE_CA_BUNDLE`. For Superset, add its CA file to the certificates Secret (step 3) as `--from-file=superset-ca.pem=<file>`, and uncomment `REQUESTS_CA_BUNDLE` in `settings.env` |
| A connection error from the judge | The model server refused the client certificate, a proxy on the way inspects TLS, or the namespace can't reach the model server |
| `401` or `403` from the model server | It expects the username header (`JUDGE_HEADERS`) or doesn't know the certificate |
| A judge request timing out | A slow model: raise the two `DEEPEVAL_..._TIMEOUT_SECONDS_OVERRIDE` settings |
| `Set real values for:` and some settings | Those settings still hold their `<...>` in `settings.env`. For `SUPERSET_HEADERS`, ask the Superset administrator which headers Superset reads |
| `401` or `403` to a request carrying the sign-in headers | Superset didn't accept `SUPERSET_HEADERS`: a header's name, the user it names, or that user doesn't exist in Superset |
| `JUDGE_CLIENT_CERT` can't be read, though the Secret has the file | The pod's user can't read the Secret's files: check `defaultMode` in `job.yaml` |
| `403` from the plugin | The account lacks the EXTENSIONS role |
| `403` or an error from SQL Lab | The account lacks the sql_lab role, or access to the database |

## After the first run

| To | Do this |
|---|---|
| Run it again | `kubectl delete job llm-eval-suite`, then `kubectl apply -f job.yaml`. A finished Job doesn't run again, and Kubernetes refuses a changed one, so delete it first |
| Change a setting | Edit `settings.env`, run `kubectl delete configmap llm-eval-suite-settings`, create it again as in step 2, and run the Job again |
| Change a certificate file or a secret value | Delete that Secret, create it again as in step 3 or 4, and run the Job again |
| Use a newer copy of the suite, or a changed golden | Build and push the image again under a new tag (step 1), put that tag on the `image:` line, and run the Job again. The code and the goldens are inside the image. A newer copy brings back `settings.env` and `job.yaml` with their `<...>`: carry your values over, and look in the new `settings.env` for any new setting |
| Keep a report | `kubectl logs job/llm-eval-suite > report.txt`, within a day of the run. The finished Job and its log are deleted after that (`ttlSecondsAfterFinished` in `job.yaml`) |
| Keep the result files | They are in the pod's own storage, and can't be copied out once the pod has finished. In `job.yaml`, replace the `results` volume's `emptyDir` with a PersistentVolumeClaim |
| Run other goldens | Change `args` in `job.yaml`. They are `run_evals.sh`'s arguments, such as `["sql", "--runs", "3"]`. A run still going after an hour is stopped (`activeDeadlineSeconds`) |

Run only `smoke` in production for now. The other goldens are written for
the dev test data, so they fail there until they are rewritten for
production's data. To judge with another model, change `JUDGE_MODEL_NAME`,
a setting like any other.

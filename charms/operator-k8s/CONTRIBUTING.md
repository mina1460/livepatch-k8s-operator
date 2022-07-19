# operator-k8s

## Developing

Create and activate a virtualenv with the development requirements:

    pip3 install virtualenv
    virtualenv -p python3 venv
    source venv/bin/activate
    pip install -r requirements-dev.txt

## Code overview

[//]: # "TODO (alesstimec) - write proper code overview."

## Intended use case

[//]: # "TODO (alesstimec) - write proper intended use case."

## Testing

The Python operator framework includes a very nice harness for testing
operator behaviour without full deployment. Just `run_tests`:

    ./run_tests

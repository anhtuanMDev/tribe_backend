PYTHON = venv/bin/python

run:
	$(PYTHON) manage.py runserver 0.0.0.0:8000

migrate:
	$(PYTHON) manage.py migrate

migrations:
	$(PYTHON) manage.py makemigrations

shell:
	$(PYTHON) manage.py shell

flush-db:
	sudo -u postgres psql -d tribe_db -c "DELETE FROM api_emailverificationcode;" -c "DELETE FROM api_verificationrequest;" -c "DELETE FROM api_user;"

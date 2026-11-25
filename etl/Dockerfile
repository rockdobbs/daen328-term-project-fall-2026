# Start from an official image that already has Python
FROM python:3.12-slim

# Every command after this runs inside /app in the image
WORKDIR /etl

# Copy the package list first and install it
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then copy your code
COPY extract.py .
COPY transform.py .
COPY load.py .
COPY tests/ tests/
COPY pytest.ini .

# What the container runs when it starts
CMD ["sh", "-c", "python extract.py && python load.py"]
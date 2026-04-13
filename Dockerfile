FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create data and logs directories
RUN mkdir -p data logs

# Expose dashboard port
EXPOSE 5050

# Default: run the bot
CMD ["python", "main.py"]

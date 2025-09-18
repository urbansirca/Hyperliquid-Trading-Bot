# Use Python 3.12 as requested
FROM python:3.12-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements.txt file into the container
COPY requirements.txt ./

# Install any needed packages specified in requirements.txt
RUN apt-get update && apt-get install -y nano && \
    pip install --no-cache-dir --trusted-host pypi.python.org -r requirements.txt

# Copy the rest of your project's source code into the container
COPY . .

# Make port available (Heroku will assign PORT dynamically)
EXPOSE $PORT

# Set environment variable for Heroku detection
ENV HEROKU=1

# Run your application when the container launches
CMD ["python", "main.py"]
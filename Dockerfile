FROM python:3.13.15-alpine3.24

# Устанавливаем рабочую директорию в контейнере
WORKDIR /app

# Копируем файлы приложения в контейнер
COPY main.py requirements.txt /app/

# Устанавливаем зависимости из файла requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Указываем команду, которая будет выполнена при запуске контейнера
CMD ["python", "main.py"]
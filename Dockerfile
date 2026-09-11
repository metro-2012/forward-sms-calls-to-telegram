FROM python:3.13.15-alpine3.24

# Устанавливаем рабочую директорию в контейнере
WORKDIR /app

# Сначала копируем зависимости для кэширования слоя
COPY requirements.txt /app/

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Теперь копируем все Python-файлы приложения (без README, .git и т.д.)
COPY *.py /app/

# Указываем команду, которая будет выполнена при запуске контейнера
CMD ["python", "main.py"]
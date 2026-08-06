"""
Модуль управления шаблонами
Отвечает за загрузку и кэширование HTML-шаблонов (Single Responsibility Principle)
"""

from pathlib import Path


class TemplateManager:
    """
    Менеджер шаблонов: загружает HTML-файлы из файловой системы
    и кэширует их содержимое для повторного использования.
    """

    DEFAULT_TEMPLATE_DIR = Path(__file__).parent / "templates"

    def __init__(self, template_dir=None):
        """
        Args:
            template_dir: Путь к директории шаблонов.
                          По умолчанию используется ./templates рядом с модулем.
        """
        self.template_dir = Path(template_dir) if template_dir else self.DEFAULT_TEMPLATE_DIR
        self._cache = {}

    def get_template(self, name):
        """
        Загрузка шаблона по имени с кэшированием.

        Args:
            name: Имя файла шаблона (например, 'index.html')

        Returns:
            str: Содержимое шаблона

        Raises:
            FileNotFoundError: Если файл шаблона не найден
        """
        if name in self._cache:
            return self._cache[name]

        template_path = self.template_dir / name

        if not template_path.exists():
            raise FileNotFoundError(
                f"Template not found: {template_path}"
            )

        content = template_path.read_text(encoding='utf-8')
        self._cache[name] = content
        return content

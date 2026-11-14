import ast, py_compile, subprocess, sys, tempfile, unittest, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / 'main.py', ROOT / 'manager.py', ROOT / 'template' / 'subscription_bot.py']


class PythonCompatibility(unittest.TestCase):
    """The README promises Python 3.11 and 3.12. Python 3.12 accepts f-strings that 3.11 rejects."""

    def test_sources_compile(self):
        for path in FILES:
            with self.subTest(file=path.name):
                py_compile.compile(str(path), cfile=str(Path(tempfile.gettempdir()) / (path.name + 'c')), doraise=True)

    @unittest.skipIf(sys.version_info < (3, 12), 'checked by the compiler itself on older Pythons')
    def test_no_fstring_syntax_that_python_311_rejects(self):
        problems = []
        for path in FILES:
            source = path.read_text(encoding='utf-8')
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.JoinedStr):
                    continue
                text = ast.get_source_segment(source, node) or ''
                quote = text.lstrip('fFrR')[:1]
                for value in node.values:
                    if isinstance(value, ast.FormattedValue):
                        inner = ast.get_source_segment(source, value.value) or ''
                        if quote in inner or '\\' in inner or '\n' in inner:
                            problems.append(f'{path.name}:{node.lineno}: {inner[:60]!r}')
        self.assertEqual(problems, [])

    def test_python_311_can_compile_if_available(self):
        interpreter = shutil.which('python3.11')
        if not interpreter:
            self.skipTest('python3.11 is not installed')
        for path in FILES:
            result = subprocess.run([interpreter, '-m', 'py_compile', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

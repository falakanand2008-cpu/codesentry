# CodeSentry

**Live demo:** https://falakanand2008-cpu.github.io/codesentry/

An automated code reviewer for Python and Java. It flags syntax errors and the loop and logic mistakes beginners make most, on the exact line, with a different alert sound for each type of problem.

## Background
During my software engineering internship at Metabo, I noticed developers were catching the same errors by hand in code reviews. I logged the errors they found, used probability charts to see which came up most, and built a Python tool that flagged them automatically with audio alerts. CodeSentry is my personal rebuild of that idea. It is new code, not the company's.

## What it catches
**Syntax:** Python syntax and indentation errors, missing semicolons in Java, unclosed brackets and strings

**Loops and logic:** infinite `while True` loops, loop counters that never change, off-by-one loops (`<= length`, `range(len(x) + 1)`), loops that count the wrong way, loops that only run once, lists changed while looping over them, `=` instead of `==` in an if, a stray `;` after if/for/while, strings compared with `==` in Java, `is` instead of `==`

**Warnings:** `== None`, mutable default arguments, bare `except`, unused variables

## How it works
- **Python:** the code is parsed into a syntax tree with Python's built-in `ast` module, and each rule walks the tree looking for its pattern.
- **Java:** a bracket stack finds unclosed or mismatched brackets, and line patterns catch the rest.
- **Website:** the same `review.py` runs in the browser using Pyodide (Python compiled to WebAssembly), so code is never uploaded anywhere.
- **Sounds:** a low buzz for syntax errors, a double beep for logic errors and a soft ping for warnings, made with the Web Audio API.
- **Statistics:** every review adds to a chart of P(mistake type) = times seen / all issues, showing which mistakes are most likely.

## Run it from the command line
```
python review.py my_code.py
python review.py MyCode.java
python review.py --stats my_project/
```
`--stats` reviews every Python and Java file in a folder and prints how likely each mistake type is.

## Built with
Python, JavaScript, HTML, CSS, Pyodide. Built with AI-assisted development.

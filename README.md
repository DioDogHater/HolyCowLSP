# HolyCowLSP
The language server for the HolyCow programming language.\
**Python 3.x is required to run the language server.**

## Features:
- C++ compatible highlighting
- Hovering (*all following features are when you hover*)
- Included files
- Macros
- Documentation preview for variables, members and methods
- Type information
- Object definition information

### Weakness
The LSP cannot compute the size of arrays, so their size the size of only one of their elements.

### TODO
- Fix that weakness
- Code completion
- Function call / construction help (text showing arguments needed and default values)

### HolyCow Compiler:
https://github.com/DioDogHater/HolyCow

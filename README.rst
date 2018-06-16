=======
Acute
=======
Tiny Qt event loop noncompliant with `PEP 3156`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
:author: Igor Kotrasiński <i.kotrasinsk@gmail.com>, forked off of Quamash by Mark Harviston
         <mark.harviston@gmail.com>, Arve Knudsen <arve.knudsen@gmail.com>

Requirements
============
Acute requires Python 3.4 or Python 3.3 with the backported ``asyncio`` library and PyQt5.

Installation
============
TODO: ``pip install acute``

Features
========
Acute is not meant to be used as a `PEP 3156` compliant event loop. Compared to quamash, it retains
the OS-independent bare minimum of 'call soon' and 'call after a delay'. Instead, it's meant to
leverage asyncio to write PyQt-style code with coroutines in place of signals.

For that purpose, acute extends its event loop with a 'call_when_done' method that accepts a set of
PyQt signals wrapped in an instance of an AsyncSignals class. This allows you to await on a set of
signals in an async method instead of making cumbersome one-time connections to functions. The
futures and tasks created by the event loop are also extended with a 'done_signal' signal, allowing
regular PyQt code to connect to a future's completion.

TODO - detailed API description.

Usage
=====

TODO

Changelog
=========

Version 0.1.0
-------------
* First version that passes extra signal tests.

Testing
=======
Acute is tested with pytest; in order to run the test suite, just install pytest
and execute py.test on the commandline. The tests themselves are beneath the 'tests' directory.

License
=======
You may use, modify, and redistribute this software under the terms of the `BSD License`.
See LICENSE.

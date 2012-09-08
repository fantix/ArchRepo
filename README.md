ArchRepo
========

Powers the Arch Linux unofficial user repositories.


What is it used for
===================

1. Manages your Arch Linux unofficial user repository

It watches a directory of your Arch Linux package files, and keep your
repository db file up to date. It is smart to detect uploading/broken package
files, preventing them from being added to the repository. Also it keeps your
package directory well-structured. Meanwhile you still have options to
synchronize everything manually at the same time without causing conflicts.


2. Present packages with web frontend

It offers an Arch-style web frontend for users to easily browse packages by
maintainer, architecture, update time and so on. It is translated into
different languages (zh_CN and en_US for now). Users will also be able to
download packages of all versions.


3. Advanced full-text search

Powered by PostgreSQL, ArchRepo allows users to search for a package in natural
languages. It is also designed for production performance - search results are
shared between different requests. This can also be tuned in configuration.


4. Social

Normal users will be able to discuss in a package, subscribe updates and so on.
Packages maintainers will also be able to get notifications about out-of-date
packages for example. It integrates with FluxBB single sign-on plugin, so users
from FluxBB simply becomes an ArchRepo user seamlessly.

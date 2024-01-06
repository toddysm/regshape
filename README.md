# RegShape Overview

![GitHub issues](https://img.shields.io/github/issues-raw/toddysm/regshape?link=https%3A%2F%2Fgithub.com%2Ftoddysm%2Fregshape%2Fissues)
 ![GitHub pull requests](https://img.shields.io/github/issues-pr-raw/toddysm/regshape?link=https%3A%2F%2Fgithub.com%2Ftoddysm%2Fregshape%2Fpulls)

RegShape (from REGistry reSHAPE) is a CLI tool and a Python library for manipulating 
artifacts in an [OCI](https://opencoutnaiers.org) registry. While there are many other tools that can do this
(see [ORAS](https://oras.land), [regclient](https://github.com/regclient/regclient)
or Google's [crane](https://github.com/google/go-containerregistry/tree/main/cmd/crane)), 
the goal of RegShape is to provide a flexibility to manipulate the requests with 
an intention to break the consistency of the artifacts.

You should be able to use RegShape similarly to the tools above, but you can also
use it in expert mode where you can manually craft the requests to the registry
and try to break it. This is useful for testing the registry implementations or
the security of the registry.

RegShape is written in Python and offers Python libraries that can be leveraged 
to build your own tools. The CLI tool is built on top of the libraries and uses
[click](https://click.palletsprojects.com/) framework.

**Note:** The tool is still in early development and the API is not stable yet.
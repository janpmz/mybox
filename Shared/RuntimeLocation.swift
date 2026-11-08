// The fork has its own installation and launchd namespace. Apple's container
// package can update /usr/local independently of MyBox.
let forkInstallRoot = "/Library/Application Support/MyBox/Runtime"
let forkPackageIdentifier = "io.github.janpmz.my-container-box.runtime"
let myboxPackageIdentifier = "io.github.janpmz.mybox"
let forkServicePrefix = "io.github.janpmz.mybox.runtime."

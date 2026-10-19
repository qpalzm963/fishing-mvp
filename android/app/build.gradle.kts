plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "io.github.qpalzm963.fishingmvp"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.qpalzm963.fishingmvp"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0-prototype"
        ndk {
            // arm64 for phones, x86_64 for desktop emulators.
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

// Bundle five small detector fixtures for the on-device self test.
val fixtureAssets = layout.buildDirectory.dir("generated/fixtureAssets")
val copyFixtures by tasks.registering(Copy::class) {
    from("../../tests/fixtures") {
        include("waiting.jpg", "prompt.jpg", "qte.jpg", "quality.jpg", "result.jpg")
    }
    into(fixtureAssets.map { it.dir("fixtures") })
}
android.sourceSets.getByName("main").assets.srcDir(fixtureAssets)
tasks.named("preBuild") { dependsOn(copyFixtures) }

chaquopy {
    defaultConfig {
        // Chaquopy's OpenCV wheel (4.5.1.48) is published for Python 3.10.
        version = "3.10"
        System.getenv("CHAQUOPY_BUILD_PYTHON")?.let { buildPython(it) }
        pip {
            install("numpy==1.26.2")
            install("opencv-python==4.5.1.48")
            install("PyYAML==6.0.3")
        }
    }
    sourceSets {
        getByName("main") {
            // Reuse the desktop detector package verbatim (repo-root src/).
            srcDir("../../src")
        }
    }
}

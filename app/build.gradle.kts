import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp)
}

// Release builds get their version from the git tag (see .github/workflows/release.yml):
//   ./gradlew assembleRelease -PappVersionName=0.2.0   ->  versionName 0.2.0, versionCode 200
val appVersionName = (findProperty("appVersionName") as String?)?.removePrefix("v") ?: "0.1.0"
val appVersionCode = appVersionName.split('.', '-').take(3).map { it.toIntOrNull() ?: 0 }
    .let { (it + listOf(0, 0, 0)).take(3) }
    .let { (major, minor, patch) -> major * 10_000 + minor * 100 + patch }
    .coerceAtLeast(1)

// The permanent release key comes from environment variables (GitHub secrets in CI).
val releaseKeystore: String? = System.getenv("SIGNING_KEYSTORE_PATH")

// Login server address: -PauthUrl=… on the command line, else channelbox.authUrl in gradle.properties.
val authUrl: String = ((findProperty("authUrl") ?: findProperty("channelbox.authUrl")) as String?)
    ?.trim()?.trimEnd('/').orEmpty()

android {
    namespace = "io.github.axon07.channelbox"
    compileSdk = 35

    defaultConfig {
        // Dan Media Box's own app id (the code keeps its original package name; that's fine).
        applicationId = "fyi.danmed.mediabox"
        // 21 = Android 5.0, so old/cheap Android TV boxes still work.
        minSdk = 21
        targetSdk = 35
        versionCode = appVersionCode
        versionName = appVersionName
        // GitHub repo the in-app updater checks for new releases.
        buildConfigField("String", "UPDATE_REPO", "\"dansolutions2017-coder/Channelbox\"")
        // Your login server (see gradle.properties → channelbox.authUrl). Empty = the original
        // Channelbox behaviour: users add their own M3U / Xtream / Stalker playlists.
        buildConfigField("String", "AUTH_URL", "\"$authUrl\"")
    }

    signingConfigs {
        if (releaseKeystore != null) {
            create("release") {
                storeFile = file(releaseKeystore)
                storePassword = System.getenv("SIGNING_STORE_PASSWORD")
                keyAlias = System.getenv("SIGNING_KEY_ALIAS")
                keyPassword = System.getenv("SIGNING_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            // R8: removes unused code (a much smaller download) and optimises the rest – Compose
            // screens and lists run noticeably faster in a minified build. Keep rules: proguard-rules.pro.
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            // Real boxes are ARM (Fire TV, Android TV, Google TV); x86 is only for emulators, which
            // use debug builds. Halves the native libraries (VPN, FFmpeg audio, RTMP).
            ndk { abiFilters += listOf("armeabi-v7a", "arm64-v8a") }
            // Official releases are signed with the permanent key so in-app updates install over
            // the old version. Local/CI test builds fall back to the debug key.
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }

    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }

    packaging {
        resources.excludes += listOf(
            "/META-INF/{AL2.0,LGPL2.1}",
            "/META-INF/versions/9/OSGI-INF/MANIFEST.MF",
            "/META-INF/DEPENDENCIES",
            "/META-INF/INDEX.LIST",
            // BouncyCastle data SMB never uses (post-quantum tables, certificate messages): ~1.3 MB.
            "org/bouncycastle/pqc/**",
            "org/bouncycastle/x509/*.properties",
        )
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)

    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.foundation)
    implementation(libs.compose.ui.tooling.preview)
    debugImplementation(libs.compose.ui.tooling)
    implementation(libs.tv.material)

    implementation(libs.media3.exoplayer)
    implementation(libs.media3.exoplayer.hls)
    implementation(libs.media3.exoplayer.dash)
    implementation(libs.media3.exoplayer.smoothstreaming)
    implementation(libs.media3.exoplayer.rtsp)
    implementation(libs.media3.datasource.rtmp)
    implementation(libs.media3.ffmpeg.decoder)
    implementation(libs.media3.ui)
    implementation(libs.media3.datasource.okhttp)

    implementation(libs.room.runtime)
    implementation(libs.room.ktx)
    ksp(libs.room.compiler)

    implementation(libs.datastore.preferences)
    implementation(libs.work.runtime.ktx)
    implementation(libs.okhttp)
    implementation(libs.okhttp.doh)
    implementation(libs.wireguard.tunnel)
    implementation(libs.smbj)
    implementation(libs.coil.compose)
    implementation(libs.zxing.core) // QR code for "add a playlist from your phone"
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.gson) // streaming JSON for Stalker portals (huge guides without filling memory)

    testImplementation(libs.junit)
    testImplementation(libs.kxml2)
    testImplementation(libs.okhttp.mockwebserver)
}

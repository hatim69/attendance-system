package com.madarwala.attendance

import android.Manifest
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ExitToApp
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.AccountBox
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.Face
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.FileProvider
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val api = Api(applicationContext)
        setContent { AppTheme { App(api) } }
    }
}

enum class Screen(val title: String) {
    Home("Home"), Attendance("Attendance"), Settings("Settings"),
    ApplyLeave("Apply for leave"), MyLeaves("My leaves"), Salary("Payslips"), Wfh("Work from home")
}

private val TABS = listOf(Screen.Home, Screen.Attendance, Screen.Settings)

/** What every screen needs: the server, navigation, and a way to show a message. */
class AppState(val api: Api, private val snackbar: SnackbarHostState, private val scope: CoroutineScope,
               val go: (Screen) -> Unit, val back: () -> Unit, val logout: () -> Unit) {
    fun toast(msg: String) {
        scope.launch { snackbar.showSnackbar(msg) }
    }

    /** Run a server call; a refusal is shown as a message, an expired login goes back to the login screen. */
    fun launch(block: suspend () -> Unit) = scope.launch { guard(block) }

    suspend fun guard(block: suspend () -> Unit) {
        try {
            block()
        } catch (e: CancellationException) {
            throw e
        } catch (e: ApiError) {
            if (e.code == 401) logout() else snackbar.showSnackbar(e.message ?: "Something went wrong")
        } catch (e: Exception) {
            snackbar.showSnackbar("Something went wrong. Please try again.")
        }
    }
}

@Composable
fun App(api: Api) {
    var loggedIn by remember { mutableStateOf(api.token != null) }
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    if (!loggedIn) {
        Scaffold(snackbarHost = { SnackbarHost(snackbar) }) { pad ->
            Box(Modifier.padding(pad)) { LoginScreen(api, snackbar) { loggedIn = true } }
        }
        return
    }
    val stack = remember { mutableStateListOf(Screen.Home) }
    val screen = stack.last()
    val state = remember {
        AppState(api, snackbar, scope,
            go = { s -> if (s in TABS) { stack.clear(); stack.add(s) } else stack.add(s) },
            back = { if (stack.size > 1) stack.removeAt(stack.size - 1) },
            logout = { Reminders.stop(api.context); api.logout(); stack.clear(); stack.add(Screen.Home); loggedIn = false })
    }
    BackHandler(enabled = stack.size > 1) { state.back() }
    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
        bottomBar = { if (screen in TABS) BottomTabs(screen, state.go) },
        containerColor = Color.White
    ) { pad ->
        Box(Modifier.padding(bottom = pad.calculateBottomPadding()).fillMaxSize()) {
            when (screen) {
                Screen.Home -> HomeScreen(state)
                Screen.Attendance -> AttendanceScreen(state)
                Screen.Settings -> SettingsScreen(state)
                Screen.ApplyLeave -> ApplyLeaveScreen(state)
                Screen.MyLeaves -> MyLeavesScreen(state)
                Screen.Salary -> SalaryScreen(state)
                Screen.Wfh -> WfhScreen(state)
            }
        }
    }
}

@Composable
fun BottomTabs(current: Screen, go: (Screen) -> Unit) {
    Column {
        Box(Modifier.fillMaxWidth().height(1.dp).background(Line))
        NavigationBar(containerColor = Color.White, tonalElevation = 0.dp) {
            listOf(Triple(Screen.Home, Icons.Filled.Home, "Home"), Triple(Screen.Attendance, Icons.Filled.DateRange, "Attendance"),
                Triple(Screen.Settings, Icons.Filled.Settings, "Settings")).forEach { (s, icon, label) ->
                NavigationBarItem(
                    selected = current == s, onClick = { go(s) },
                    icon = { Icon(icon, label) },
                    label = { Text(label, fontWeight = if (current == s) FontWeight.SemiBold else FontWeight.Normal) },
                    colors = NavigationBarItemDefaults.colors(
                        selectedIconColor = Accent, selectedTextColor = Accent, indicatorColor = AccentSoft,
                        unselectedIconColor = Muted, unselectedTextColor = Muted)
                )
            }
        }
    }
}

@Composable
fun LoginScreen(api: Api, snackbar: SnackbarHostState, onDone: () -> Unit) {
    var id by remember { mutableStateOf("") }
    var pin by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).imePadding()) {
        Column(Modifier.fillMaxWidth().statusBarsPadding().padding(top = 64.dp, bottom = 24.dp),
            horizontalAlignment = Alignment.CenterHorizontally) {
            Box(Modifier.size(84.dp).clip(RoundedCornerShape(24.dp)).background(Brand), contentAlignment = Alignment.Center) {
                // the launcher icon is an adaptive icon (XML), which Compose cannot draw: use its foreground picture
                Image(painterResource(R.mipmap.ic_launcher_foreground), null, Modifier.size(84.dp))
            }
            Spacer(Modifier.height(18.dp))
            Text("Madarwala Attendance", color = Ink, fontSize = 24.sp, fontWeight = FontWeight.Bold)
            Text("Madarwala Tours and Travels", color = Muted, fontSize = 14.sp)
        }
        Column(Modifier.padding(24.dp)) {
            OutlinedTextField(id, { id = it.filter { c -> c.isLetterOrDigit() } }, label = { Text("Employee ID") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.fillMaxWidth())
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(pin, { pin = it.filter { c -> c.isDigit() }.take(8) }, label = { Text("PIN") }, singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword), modifier = Modifier.fillMaxWidth())
            Spacer(Modifier.height(24.dp))
            Button(onClick = {
                busy = true
                scope.launch {
                    try {
                        api.login(id, pin)
                        onDone()
                    } catch (e: ApiError) {
                        snackbar.showSnackbar(e.message ?: "Could not log in")
                    } finally {
                        busy = false
                    }
                }
            }, enabled = !busy && id.isNotBlank() && pin.length >= 4, shape = RoundedCornerShape(12.dp),
                modifier = Modifier.fillMaxWidth().height(52.dp)) {
                if (busy) CircularProgressIndicator(color = Color.White, modifier = Modifier.size(22.dp), strokeWidth = 2.dp)
                else Text("Log in", fontSize = 17.sp)
            }
            Spacer(Modifier.height(16.dp))
            Text("Your employee ID is the one on the attendance machine. Ask HR for your PIN.", color = Muted,
                fontSize = 13.sp, textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth())
        }
    }
}

@Composable
fun HomeScreen(s: AppState) {
    val context = LocalContext.current
    var me by remember { mutableStateOf<JSONObject?>(null) }
    var lates by remember { mutableStateOf<JSONObject?>(null) }
    var now by remember { mutableStateOf(Date()) }
    var working by remember { mutableStateOf<String?>(null) }   // what the punch is doing right now
    var reload by remember { mutableStateOf(0) }
    var month by remember { mutableStateOf<Triple<Double, Double, Double>?>(null) }
    LaunchedEffect(reload) {
        s.guard { me = s.api.getObj("/api/app/me") }
        s.guard { lates = s.api.getObj("/api/app/lates") }
        s.guard {
            val a = s.api.getArr("/api/app/attendance")
            val codes = (0 until a.length()).map { a.getJSONObject(it).optString("code") }
            month = Triple(codes.count { it in setOf("P", "P-LT", "POW") } + 0.5 * codes.count { it.startsWith("P/2") },
                codes.count { it == "P-LT" }.toDouble(), codes.count { it == "A" }.toDouble())
        }
    }
    LaunchedEffect(Unit) { while (true) { now = Date(); delay(1000) } }
    val notifyPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { Reminders.start(context) }
    LaunchedEffect(Unit) {   // reminders: ask once for permission to show notifications (Android 13+), then start checking
        if (!Reminders.allowed(context) && android.os.Build.VERSION.SDK_INT >= 33) notifyPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        else Reminders.start(context)
    }
    val scope = rememberCoroutineScope()
    val photo = selfieFile(context)

    val camera = rememberLauncherForActivityResult(TakeSelfie()) { ok ->
        if (!ok) { working = null; return@rememberLauncherForActivityResult }
        scope.launch {
            s.guard {
                try {
                    val bytes = selfieBytes(photo) ?: throw ApiError(0, "The photo could not be read. Please try again.")
                    working = "Finding your location…"
                    val loc = currentLocation(context)
                        ?: throw ApiError(0, "Could not find your location. Go near a window or outside and try again.")
                    working = "Saving your attendance…"
                    val r = s.api.punch(loc.latitude, loc.longitude, loc.accuracy, bytes)
                    val at = r.optString("time").let { if (it.length >= 16) it.substring(11, 16) else it }
                    val where = r.optString("site").takeIf { it.isNotBlank() && it != "null" }?.let { " · $it" } ?: ""
                    s.toast("${r.optString("type")} at $at$where")
                    reload++
                } finally {
                    working = null
                    photo.delete()
                }
            }
        }
    }
    fun startCamera() {
        if (!locationOn(context)) { s.toast("Turn on Location (GPS) in your phone settings, then try again."); return }
        working = "Opening the camera…"
        photo.delete()
        val uri = FileProvider.getUriForFile(context, "com.madarwala.attendance.fileprovider", photo)
        try { camera.launch(uri) } catch (e: Exception) { working = null; s.toast("No camera app found on this phone.") }
    }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
        if (granted.values.any { it }) startCamera() else s.toast("Allow location so your attendance can be marked at the office.")
    }

    val today = me?.optJSONArray("today")
    val count = today?.length() ?: 0
    val times = (0 until count).map { today!!.getJSONObject(it).getString("ts").substring(11, 16) }
    val hour = Calendar.getInstance().get(Calendar.HOUR_OF_DAY)
    Column(Modifier.fillMaxSize().background(Page).verticalScroll(rememberScrollState()).statusBarsPadding()) {
        Row(Modifier.fillMaxWidth().padding(20.dp, 20.dp, 20.dp, 12.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(if (hour < 12) "Good morning" else if (hour < 17) "Good afternoon" else "Good evening", color = Muted, fontSize = 15.sp)
                Text(s.api.name, color = Ink, fontSize = 24.sp, fontWeight = FontWeight.Bold)
            }
            Box(Modifier.size(48.dp).clip(CircleShape).background(AccentSoft).clickable { s.go(Screen.Settings) },
                contentAlignment = Alignment.Center) {
                Text(initials(s.api.name), color = Accent, fontWeight = FontWeight.Bold, fontSize = 17.sp)
            }
        }
        // today: the clock, the day's in / out times and the check-in button
        Card(Modifier.padding(horizontal = 16.dp).fillMaxWidth(), shape = RoundedCornerShape(24.dp),
            colors = CardDefaults.cardColors(containerColor = Color.White), elevation = CardDefaults.cardElevation(1.dp)) {
            Column(Modifier.padding(20.dp)) {
                Row(verticalAlignment = Alignment.Bottom) {
                    Column(Modifier.weight(1f)) {
                        Text("Today", color = Muted, fontSize = 13.sp)
                        Text(SimpleDateFormat("EEEE, d MMM", Locale.ENGLISH).format(now), color = Ink, fontSize = 17.sp, fontWeight = FontWeight.SemiBold)
                    }
                    Text(SimpleDateFormat("h:mm a", Locale.ENGLISH).format(now), color = Ink, fontSize = 26.sp, fontWeight = FontWeight.Bold)
                }
                Spacer(Modifier.height(16.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    TimeBox("Checked in", times.firstOrNull(), Good, Modifier.weight(1f))
                    TimeBox("Checked out", if (count > 1 && count % 2 == 0) times.last() else null, Accent, Modifier.weight(1f))
                }
                Spacer(Modifier.height(16.dp))
                Button(onClick = {
                    if (hasLocationPermission(context)) startCamera()
                    else permission.launch(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION))
                }, enabled = working == null && me != null, shape = RoundedCornerShape(16.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = if (count % 2 == 0) Accent else Ink),
                    modifier = Modifier.fillMaxWidth().height(58.dp)) {
                    if (working != null) {
                        CircularProgressIndicator(color = Color.White, modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.width(12.dp))
                        Text(working ?: "", fontSize = 15.sp)
                    } else {
                        Icon(Icons.Filled.Face, null, modifier = Modifier.size(22.dp))
                        Spacer(Modifier.width(10.dp))
                        Text(if (count % 2 == 0) "Check in" else "Check out", fontSize = 17.sp, fontWeight = FontWeight.SemiBold)
                    }
                }
                Text("A selfie and your location are recorded with each check-in and check-out.", color = Muted, fontSize = 12.sp,
                    textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth().padding(top = 10.dp))
                lates?.let { l ->
                    val n = l.optInt("count"); val lim = l.optInt("limit", 3)
                    if (n > 0) {
                        Text("Late arrivals: $n of $lim allowed (${l.optString("period_label").lowercase()})",
                            color = if (n >= lim) Bad else Color(0xFFB45309), fontSize = 13.sp, fontWeight = FontWeight.Medium,
                            textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth().padding(top = 6.dp))
                    }
                }
            }
        }
        // this month at a glance
        month?.let { m ->
            Row(Modifier.padding(16.dp, 16.dp, 16.dp, 0.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Stat("Present", m.first, Good, Modifier.weight(1f))
                Stat("Late", m.second, Color(0xFFB45309), Modifier.weight(1f))
                Stat("Absent", m.third, Bad, Modifier.weight(1f))
            }
        }
        Text("Quick actions", color = Muted, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(20.dp, 20.dp, 20.dp, 8.dp))
        Card(Modifier.padding(horizontal = 16.dp).fillMaxWidth(), shape = RoundedCornerShape(20.dp),
            colors = CardDefaults.cardColors(containerColor = Color.White)) {
            ActionRow("My attendance", "Every day of the month: in, out, hours", Icons.Filled.DateRange, Accent) { s.go(Screen.Attendance) }
            HorizontalDivider(color = Line, modifier = Modifier.padding(start = 74.dp))
            ActionRow("Apply for leave", "Informed or Adhoc, full or half day", Icons.Filled.Edit, Color(0xFFD97706)) { s.go(Screen.ApplyLeave) }
            HorizontalDivider(color = Line, modifier = Modifier.padding(start = 74.dp))
            ActionRow("My leaves", "Balance and history", Icons.AutoMirrored.Filled.List, Color(0xFF7C3AED)) { s.go(Screen.MyLeaves) }
            HorizontalDivider(color = Line, modifier = Modifier.padding(start = 74.dp))
            ActionRow("Payslips", "Monthly salary slips", Icons.Filled.AccountBox, Good) { s.go(Screen.Salary) }
            HorizontalDivider(color = Line, modifier = Modifier.padding(start = 74.dp))
            ActionRow("Work from home", "Ask for a day at home", Icons.Filled.Home, Color(0xFF0891B2)) { s.go(Screen.Wfh) }
        }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun TimeBox(label: String, time: String?, color: Color, modifier: Modifier) {
    Column(modifier.clip(RoundedCornerShape(14.dp)).background(Page).padding(12.dp)) {
        Text(label, color = Muted, fontSize = 12.sp)
        Text(time ?: "--:--", color = if (time == null) Muted else color, fontSize = 22.sp, fontWeight = FontWeight.Bold)
    }
}

@Composable
private fun Stat(label: String, value: Double, color: Color, modifier: Modifier) {
    Column(modifier.clip(RoundedCornerShape(16.dp)).background(Color.White).padding(vertical = 12.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        Text(num(value), color = color, fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Text("$label this month", color = Muted, fontSize = 11.sp)
    }
}

fun initials(name: String): String =
    name.split(" ").filter { it.isNotBlank() && it.first().isLetter() }.take(2).joinToString("") { it.first().uppercase() }.ifEmpty { "?" }

@Composable
fun SettingsScreen(s: AppState) {
    Column(Modifier.fillMaxSize().background(Page).verticalScroll(rememberScrollState())) {
        TitleBar("Settings", null)
        Card(Modifier.padding(16.dp).fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = Color.White)) {
            Column(Modifier.padding(16.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.size(56.dp).clip(CircleShape).background(Brand), contentAlignment = Alignment.Center) {
                        Text(initials(s.api.name), color = Color.White, fontWeight = FontWeight.Bold, fontSize = 20.sp)
                    }
                    Spacer(Modifier.size(14.dp))
                    Column {
                        Text(s.api.name, fontSize = 18.sp, fontWeight = FontWeight.SemiBold, color = Ink)
                        Text("Employee ID ${s.api.employeeId}", color = Muted)
                    }
                }
                Spacer(Modifier.height(12.dp))
                HorizontalDivider(color = Line)
                KeyValue("Reminders", if (Reminders.allowed(LocalContext.current)) "On" else "Off: allow notifications for this app in phone Settings")
                KeyValue("Company", "Madarwala Tours and Travels")
                KeyValue("App version", BuildConfig.VERSION_NAME)
            }
        }
        Card(Modifier.padding(horizontal = 16.dp).fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = Color.White)) {
            Column(Modifier.padding(16.dp)) {
                Text("Need help?", fontWeight = FontWeight.SemiBold, color = Ink)
                Spacer(Modifier.height(4.dp))
                Text("For a wrong punch, a forgotten PIN or a leave question, contact HR (hr@madarwala.com).", color = Muted, fontSize = 14.sp)
            }
        }
        Spacer(Modifier.height(16.dp))
        OutlinedButton(onClick = s.logout, modifier = Modifier.padding(horizontal = 16.dp).fillMaxWidth().height(50.dp),
            shape = RoundedCornerShape(12.dp), colors = ButtonDefaults.outlinedButtonColors(contentColor = Bad)) {
            Icon(Icons.AutoMirrored.Filled.ExitToApp, null)
            Spacer(Modifier.size(8.dp))
            Text("Log out")
        }
    }
}

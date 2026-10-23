package com.madarwala.attendance

import android.content.ActivityNotFoundException
import android.content.Intent
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
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowLeft
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.FileProvider
import kotlinx.coroutines.delay
import org.json.JSONArray
import org.json.JSONObject
import java.util.Calendar

private fun JSONArray.objects(): List<JSONObject> = (0 until length()).map { getJSONObject(it) }

// ---------------------------------------------------------------- Attendance: the month, day by day

@Composable
fun AttendanceScreen(s: AppState) {
    val cal = remember { Calendar.getInstance() }
    var year by remember { mutableStateOf(cal.get(Calendar.YEAR)) }
    var month by remember { mutableStateOf(cal.get(Calendar.MONTH) + 1) }
    var rows by remember { mutableStateOf<List<JSONObject>?>(null) }
    LaunchedEffect(year, month) {
        rows = null
        s.guard { rows = s.api.getArr("/api/app/attendance?month=%04d-%02d".format(year, month)).objects() }
        if (rows == null) rows = emptyList()
    }
    fun shift(by: Int) {
        var m = month + by; var y = year
        if (m == 0) { m = 12; y-- } else if (m == 13) { m = 1; y++ }
        month = m; year = y
    }
    Column(Modifier.fillMaxSize().background(Color.White)) {
        TitleBar("Attendance", null)
        Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
            IconButton({ shift(-1) }) { Icon(Icons.AutoMirrored.Filled.KeyboardArrowLeft, "Previous month", tint = Muted, modifier = Modifier.size(32.dp)) }
            Text("${MONTHS[month - 1]} $year", color = Accent, fontSize = 22.sp, fontWeight = FontWeight.SemiBold,
                textAlign = TextAlign.Center, modifier = Modifier.weight(1f))
            IconButton({ shift(1) }) { Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, "Next month", tint = Muted, modifier = Modifier.size(32.dp)) }
        }
        val list = rows
        if (list == null) { Loading(); return@Column }
        val shown = list.filter { it.optString("code").isNotEmpty() }
        if (shown.isEmpty()) { Empty("No attendance for this month yet."); return@Column }
        // the month's totals
        fun c(vararg codes: String) = shown.count { it.optString("code") in codes }
        Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Summary("Present", c("P", "P-LT", "POW") + c("P/2", "P/2-IL", "P/2-AL") * 0.5, Good, Modifier.weight(1f))
            Summary("Late", c("P-LT").toDouble(), Color(0xFFB45309), Modifier.weight(1f))
            Summary("Absent", c("A").toDouble(), Bad, Modifier.weight(1f))
            Summary("Leave", c("IL", "AL") + c("P/2-IL", "P/2-AL") * 0.5, Color(0xFF7C3AED), Modifier.weight(1f))
        }
        Spacer(Modifier.height(10.dp))
        val head = Modifier.padding(vertical = 12.dp)
        Row(Modifier.fillMaxWidth().background(AccentSoft).padding(horizontal = 8.dp)) {
            listOf("Date" to 1.3f, "In" to 1f, "Out" to 1f, "Hrs" to 0.9f, "Status" to 1.1f).forEach { (t, w) ->
                Text(t, fontWeight = FontWeight.SemiBold, color = Ink, textAlign = TextAlign.Center, modifier = head.weight(w))
            }
        }
        LazyColumn(Modifier.fillMaxSize()) {
            items(shown) { r ->
                val code = r.optString("code")
                val (bg, fg) = when (code) {
                    "IL", "P/2-IL" -> InformedFill to Ink
                    "AL", "P/2-AL" -> AdhocFill to Ink
                    "A" -> (if (r.optString("note").contains("Adhoc")) AdhocFill else Color(0xFFFEE2E2)) to Bad
                    "P-LT" -> Color(0xFFFEF3C7) to Color(0xFFB45309)
                    "WO", "HL" -> Color(0xFFF1F5F9) to Muted
                    "IN" -> Color(0xFFE0F2FE) to Color(0xFF0369A1)
                    else -> Color(0xFFDCFCE7) to Good
                }
                Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1.3f), horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(dmy(r.getString("date")).substring(0, 5), color = Ink, fontWeight = FontWeight.Medium)
                        Text(r.optString("weekday").take(3), color = Muted, fontSize = 12.sp)
                    }
                    Text(r.optString("in").ifEmpty { "—" }, textAlign = TextAlign.Center, color = if (code == "P-LT") Color(0xFFB45309) else Ink, modifier = Modifier.weight(1f))
                    Text(r.optString("out").ifEmpty { "—" }, textAlign = TextAlign.Center, color = Ink, modifier = Modifier.weight(1f))
                    Text(r.optString("hours").ifEmpty { "—" }, textAlign = TextAlign.Center, color = Ink, modifier = Modifier.weight(0.9f))
                    Box(Modifier.weight(1.1f), contentAlignment = Alignment.Center) { Pill(code, bg, fg) }
                }
                HorizontalDivider(color = Line)
            }
            item {
                Text("P present · P-LT late · A absent · WO weekly off · HL holiday · IL Informed leave · AL Adhoc leave · " +
                        "P/2 half day · IN at work now", color = Muted, fontSize = 12.sp, modifier = Modifier.padding(16.dp))
            }
        }
    }
}

@Composable
private fun Summary(label: String, value: Double, color: Color, modifier: Modifier) {
    Column(modifier.clip(RoundedCornerShape(12.dp)).background(Page).padding(vertical = 10.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Text(num(value), color = color, fontSize = 20.sp, fontWeight = FontWeight.Bold)
        Text(label, color = Muted, fontSize = 12.sp)
    }
}

// ---------------------------------------------------------------- Apply for leave

@Composable
private fun DateBox(label: String, value: String, enabled: Boolean = true, modifier: Modifier, onPick: (String) -> Unit) {
    val context = LocalContext.current
    Column(modifier.clip(RoundedCornerShape(12.dp)).border(1.dp, if (enabled) Muted else Line, RoundedCornerShape(12.dp))
        .clickable(enabled = enabled) { pickDate(context, value) { onPick(it) } }.padding(14.dp)) {
        Text(label, color = Muted, fontSize = 12.sp)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(if (value.isEmpty()) "Choose" else dmy(value), color = if (enabled) Ink else Muted, fontSize = 17.sp, modifier = Modifier.weight(1f))
            Icon(Icons.Filled.DateRange, null, tint = Muted, modifier = Modifier.size(20.dp))
        }
    }
}

@Composable
private fun Choice(text: String, selected: Boolean, color: Color, modifier: Modifier, onClick: () -> Unit) {
    Box(modifier.clip(RoundedCornerShape(12.dp)).background(if (selected) color else Color.White)
        .border(if (selected) 2.dp else 1.dp, if (selected) Ink else Line, RoundedCornerShape(12.dp))
        .clickable(onClick = onClick).padding(vertical = 14.dp), contentAlignment = Alignment.Center) {
        Text(text, color = Ink, fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal)
    }
}

@Composable
fun ApplyLeaveScreen(s: AppState) {
    var from by remember { mutableStateOf("") }
    var to by remember { mutableStateOf("") }
    var type by remember { mutableStateOf("Informed") }
    var half by remember { mutableStateOf(false) }
    var reason by remember { mutableStateOf("") }
    var consent by remember { mutableStateOf("") }
    var plan by remember { mutableStateOf<JSONObject?>(null) }
    var problem by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val end = if (half || to.isEmpty()) from else to
    LaunchedEffect(from, end, type, half) {
        plan = null; problem = null
        if (from.isEmpty()) return@LaunchedEffect
        delay(300)
        try {
            plan = s.api.getObj("/api/app/leave/preview?type=$type&start_date=$from&end_date=$end&half=$half")
        } catch (e: ApiError) {
            if (e.code == 401) s.logout() else problem = e.message
        } catch (e: Exception) {
            problem = "Could not check this leave. Please try again."
        }
    }
    Column(Modifier.fillMaxSize().background(Color.White)) {
        TitleBar("Apply for leave", s.back)
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).imePadding().padding(20.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                DateBox("From date", from, modifier = Modifier.weight(1f)) { from = it; if (to.isNotEmpty() && to < it) to = it }
                DateBox("To date", if (half) from else to, enabled = !half, modifier = Modifier.weight(1f)) { to = it }
            }
            Spacer(Modifier.height(16.dp))
            Text("Leave type", color = Muted, fontSize = 13.sp)
            Spacer(Modifier.height(6.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Choice("Informed", type == "Informed", InformedFill, Modifier.weight(1f)) { type = "Informed" }
                Choice("Adhoc", type == "Adhoc", AdhocFill, Modifier.weight(1f)) { type = "Adhoc" }
            }
            Text("Informed: planned leave, from your Informed balance. Adhoc: emergency leave, from your Adhoc balance.", color = Muted, fontSize = 12.sp,
                modifier = Modifier.padding(top = 6.dp))
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 8.dp).clickable { half = !half }) {
                Checkbox(half, { half = it })
                Text("Half day (one date)", color = Ink)
            }
            OutlinedTextField(reason, { reason = it.take(200) }, label = { Text("Reason") }, minLines = 3, modifier = Modifier.fillMaxWidth())
            Spacer(Modifier.height(16.dp))
            problem?.let { Text(it, color = Bad, fontSize = 14.sp) }
            plan?.let { p ->
                Card(colors = CardDefaults.cardColors(containerColor = Page), modifier = Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(14.dp)) {
                        KeyValue("Working days", num(p.optDouble("working_days")))
                        if (p.optDouble("informed_days") > 0) KeyValue("From Informed balance", num(p.optDouble("informed_days")))
                        if (p.optDouble("adhoc_days") > 0) KeyValue("From Adhoc balance", num(p.optDouble("adhoc_days")))
                        if (p.optInt("excess_days") > 0) {
                            KeyValue("Beyond your balance", "${p.optInt("excess_days")} day(s)")
                            if (p.optDouble("deduction") > 0) KeyValue("Salary cut", rupees(p.optDouble("deduction")))
                        }
                        p.optJSONArray("warnings")?.let { w ->
                            for (i in 0 until w.length()) Text("• " + w.getString(i), color = Color(0xFFB45309), fontSize = 13.sp, modifier = Modifier.padding(top = 4.dp))
                        }
                    }
                }
                if (p.optBoolean("needs_consent")) {
                    Spacer(Modifier.height(12.dp))
                    Text(p.optString("consent_text"), color = Ink, fontSize = 13.sp)
                    OutlinedTextField(consent, { consent = it }, label = { Text("Type your full name to agree") }, singleLine = true,
                        modifier = Modifier.fillMaxWidth().padding(top = 6.dp))
                }
            }
            Spacer(Modifier.height(20.dp))
            Button(onClick = {
                busy = true
                s.launch {
                    try {
                        val body = JSONObject().put("start_date", from).put("end_date", end).put("type", type)
                            .put("reason", reason).put("half", half).put("consent_name", consent)
                        s.api.post("/api/app/leave", body)
                        s.toast("Leave applied. HR will approve it soon.")
                        s.back()
                        s.go(Screen.MyLeaves)
                    } finally { busy = false }
                }
            }, enabled = !busy && plan != null && (plan?.optBoolean("needs_consent") != true || consent.isNotBlank()),
                shape = RoundedCornerShape(12.dp), modifier = Modifier.fillMaxWidth().height(52.dp)) {
                if (busy) CircularProgressIndicator(color = Color.White, modifier = Modifier.size(22.dp), strokeWidth = 2.dp)
                else Text("Apply for leave", fontSize = 17.sp)
            }
        }
    }
}

// ---------------------------------------------------------------- My leaves: balance and history

@Composable
fun MyLeavesScreen(s: AppState) {
    var bal by remember { mutableStateOf<JSONObject?>(null) }
    var list by remember { mutableStateOf<List<JSONObject>?>(null) }
    var reload by remember { mutableStateOf(0) }
    LaunchedEffect(reload) {
        s.guard { bal = s.api.getObj("/api/app/leave-balance") }
        s.guard { list = s.api.getArr("/api/app/leaves").objects() }
        if (list == null) list = emptyList()
    }
    Column(Modifier.fillMaxSize().background(Color.White)) {
        TitleBar("My leaves", s.back)
        val b = bal
        if (b == null && list == null) { Loading(); return@Column }
        LazyColumn(Modifier.fillMaxSize()) {
            if (b != null) item {
                val inf = b.optJSONObject("informed") ?: JSONObject()
                val adh = b.optJSONObject("adhoc") ?: JSONObject()
                val left = inf.optDouble("left", 0.0) + adh.optDouble("left", 0.0)
                val credited = inf.optDouble("credited", 0.0) + adh.optDouble("credited", 0.0)
                Column(Modifier.fillMaxWidth().padding(20.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                    Ring(left, credited, 190.dp, 14.dp, Accent) {
                        Text("Balance", fontSize = 18.sp, color = Muted)
                        Text(num(left), fontSize = 40.sp, fontWeight = FontWeight.Bold, color = Ink)
                    }
                    Spacer(Modifier.height(18.dp))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
                        listOf(Triple("Informed", inf, Color(0xFFEAB308)), Triple("Adhoc", adh, Bad)).forEach { (label, o, color) ->
                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                Ring(o.optDouble("left", 0.0), o.optDouble("credited", 0.0), 84.dp, 8.dp, color) {
                                    Text(num(o.optDouble("left", 0.0)), fontSize = 20.sp, fontWeight = FontWeight.Bold, color = Ink)
                                }
                                Spacer(Modifier.height(6.dp))
                                Pill(label, leaveColor(label))
                                Text("used ${num(o.optDouble("used", 0.0))} of ${num(o.optDouble("credited", 0.0))}", color = Muted, fontSize = 12.sp)
                            }
                        }
                    }
                    Spacer(Modifier.height(14.dp))
                    val note = when {
                        !b.optBoolean("eligible", true) -> "Leave starts after 3 months of work: from ${dmy(b.optString("eligible_from"))}."
                        b.optJSONObject("next_credit") != null -> b.getJSONObject("next_credit").let {
                            "Next credit on ${dmy(it.optString("date"))}: ${it.optInt("informed")} Informed" +
                                    (if (it.optInt("adhoc") > 0) " + ${it.optInt("adhoc")} Adhoc" else "")
                        }
                        else -> ""
                    }
                    if (note.isNotEmpty()) Text(note, color = Muted, fontSize = 13.sp, textAlign = TextAlign.Center)
                    Spacer(Modifier.height(14.dp))
                    Button({ s.go(Screen.ApplyLeave) }, shape = RoundedCornerShape(12.dp), modifier = Modifier.fillMaxWidth().height(50.dp)) {
                        Text("Apply for leave", fontSize = 16.sp)
                    }
                }
                Text("Leave history", fontWeight = FontWeight.SemiBold, fontSize = 17.sp, color = Ink, modifier = Modifier.padding(horizontal = 20.dp))
            }
            val l = list.orEmpty()
            if (l.isEmpty()) item { Empty("No leave yet.") }
            items(l) { r ->
                Card(Modifier.padding(horizontal = 16.dp, vertical = 6.dp).fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = Page)) {
                    Column(Modifier.padding(14.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Pill(r.optString("type") + if (r.optInt("half") == 1) " · ½ day" else "", leaveColor(r.optString("type")))
                            Spacer(Modifier.weight(1f))
                            val (bg, fg) = statusPill(r.optString("status"))
                            Pill(r.optString("status").replaceFirstChar { it.uppercase() }, bg, fg)
                        }
                        Spacer(Modifier.height(8.dp))
                        val one = r.optString("start_date") == r.optString("end_date")
                        Text(if (one) dmy(r.optString("start_date")) else "${dmy(r.optString("start_date"))} to ${dmy(r.optString("end_date"))}",
                            color = Ink, fontWeight = FontWeight.Medium)
                        if (r.optDouble("lop", 0.0) > 0) Text("Beyond balance: ${num(r.optDouble("lop"))}× day's salary cut", color = Bad, fontSize = 13.sp)
                        r.optString("note").takeIf { it.isNotBlank() && it != "null" }?.let { Text(it, color = Muted, fontSize = 13.sp) }
                        if (r.optString("status") == "pending") {
                            TextButton({ s.launch { s.api.delete("/api/app/leave/${r.getInt("id")}"); s.toast("Leave application cancelled"); reload++ } },
                                modifier = Modifier.align(Alignment.End)) { Text("Cancel application", color = Bad) }
                        }
                    }
                }
            }
            item { Spacer(Modifier.height(20.dp)) }
        }
    }
}

// ---------------------------------------------------------------- Salary: published payslips

@Composable
fun SalaryScreen(s: AppState) {
    val context = LocalContext.current
    var list by remember { mutableStateOf<List<JSONObject>?>(null) }
    var opening by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(Unit) {
        s.guard { list = s.api.getArr("/api/app/payslips").objects() }
        if (list == null) list = emptyList()
    }
    Column(Modifier.fillMaxSize().background(Color.White)) {
        TitleBar("Payslips", s.back)
        val l = list
        if (l == null) { Loading(); return@Column }
        if (l.isEmpty()) { Empty("No payslips yet. They appear here once HR publishes the month's salary."); return@Column }
        LazyColumn {
            items(l) { p ->
                val m = p.getString("month")
                Row(Modifier.fillMaxWidth().clickable(enabled = opening == null) {
                    opening = m
                    s.launch {
                        try {
                            val f = s.api.payslip(m, context.cacheDir)
                            val uri = FileProvider.getUriForFile(context, "com.madarwala.attendance.fileprovider", f)
                            try {
                                context.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/pdf")
                                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
                            } catch (e: ActivityNotFoundException) {
                                s.toast("Install a PDF viewer to open payslips.")
                            }
                        } finally { opening = null }
                    }
                }.padding(20.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("${MONTHS[m.substring(5, 7).toInt() - 1]} ${m.substring(0, 4)}", fontWeight = FontWeight.SemiBold, color = Ink, fontSize = 17.sp)
                        Text("Tap to open the payslip", color = Muted, fontSize = 13.sp)
                    }
                    if (opening == m) CircularProgressIndicator(Modifier.size(22.dp), color = Accent, strokeWidth = 2.dp)
                    else Text(rupees(p.optDouble("net_pay")), color = Good, fontWeight = FontWeight.Bold, fontSize = 17.sp)
                }
                HorizontalDivider(color = Line)
            }
        }
    }
}

// ---------------------------------------------------------------- Work from home requests

@Composable
fun WfhScreen(s: AppState) {
    var from by remember { mutableStateOf(todayIso()) }
    var to by remember { mutableStateOf("") }
    var reason by remember { mutableStateOf("") }
    var list by remember { mutableStateOf<List<JSONObject>?>(null) }
    var reload by remember { mutableStateOf(0) }
    var busy by remember { mutableStateOf(false) }
    LaunchedEffect(reload) {
        s.guard { list = s.api.getArr("/api/app/wfh").objects() }
        if (list == null) list = emptyList()
    }
    Column(Modifier.fillMaxSize().background(Color.White)) {
        TitleBar("Work from home", s.back)
        LazyColumn(Modifier.fillMaxSize().imePadding()) {
            item {
                Column(Modifier.padding(20.dp)) {
                    Text("Ask to work from home. Once your team lead or HR approves it, you can check in from anywhere that day " +
                            "(paid at 0.8 of a day).", color = Muted, fontSize = 14.sp)
                    Spacer(Modifier.height(14.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        DateBox("From date", from, modifier = Modifier.weight(1f)) { from = it; if (to.isNotEmpty() && to < it) to = it }
                        DateBox("To date", to.ifEmpty { from }, modifier = Modifier.weight(1f)) { to = it }
                    }
                    Spacer(Modifier.height(12.dp))
                    OutlinedTextField(reason, { reason = it.take(200) }, label = { Text("Reason") }, minLines = 2, modifier = Modifier.fillMaxWidth())
                    Spacer(Modifier.height(14.dp))
                    Button(onClick = {
                        busy = true
                        s.launch {
                            try {
                                s.api.post("/api/app/wfh", JSONObject().put("start_date", from).put("end_date", to.ifEmpty { from }).put("reason", reason))
                                s.toast("Request sent for approval")
                                reason = ""; reload++
                            } finally { busy = false }
                        }
                    }, enabled = !busy, shape = RoundedCornerShape(12.dp), modifier = Modifier.fillMaxWidth().height(50.dp)) { Text("Send request", fontSize = 16.sp) }
                }
                Text("My requests", fontWeight = FontWeight.SemiBold, fontSize = 17.sp, color = Ink, modifier = Modifier.padding(horizontal = 20.dp))
            }
            val l = list
            if (l == null) item { Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator(color = Accent) } }
            else if (l.isEmpty()) item { Empty("No requests yet.") }
            else items(l) { r ->
                Row(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        val one = r.optString("start_date") == r.optString("end_date")
                        Text(if (one) dmy(r.optString("start_date")) else "${dmy(r.optString("start_date"))} to ${dmy(r.optString("end_date"))}",
                            color = Ink, fontWeight = FontWeight.Medium)
                        r.optString("reason").takeIf { it.isNotBlank() }?.let { Text(it, color = Muted, fontSize = 13.sp) }
                        if (r.optString("status") == "pending") {
                            Text("Cancel", color = Bad, fontSize = 13.sp, modifier = Modifier.padding(top = 4.dp).clickable {
                                s.launch { s.api.delete("/api/app/wfh/${r.getInt("id")}"); s.toast("Request cancelled"); reload++ }
                            })
                        }
                    }
                    val (bg, fg) = statusPill(r.optString("status"))
                    Pill(r.optString("status").replaceFirstChar { it.uppercase() }, bg, fg)
                }
                HorizontalDivider(color = Line, modifier = Modifier.padding(horizontal = 20.dp))
            }
            item { Spacer(Modifier.width(1.dp).height(24.dp)) }
        }
    }
}

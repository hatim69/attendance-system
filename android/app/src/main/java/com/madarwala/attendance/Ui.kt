package com.madarwala.attendance

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

val Accent = Color(0xFF4F46E5)        // indigo, as on the attendance dashboard
val AccentLight = Color(0xFF6366F1)
val AccentDark = Color(0xFF3730A3)
val AccentSoft = Color(0xFFEEF2FF)
val Ink = Color(0xFF1F2937)
val Muted = Color(0xFF6B7280)
val Line = Color(0xFFE5E7EB)
val Page = Color(0xFFF5F6FA)
val InformedFill = Color(0xFFFFEB9C)   // Informed leave: yellow, as on the dashboard and in the Excel reports
val AdhocFill = Color(0xFFFFC7CE)      // Adhoc leave: red
val Good = Color(0xFF16A34A)
val Bad = Color(0xFFDC2626)
val Brand = Brush.linearGradient(listOf(AccentLight, AccentDark), Offset.Zero, Offset(900f, 300f))

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = lightColorScheme(primary = Accent, onPrimary = Color.White, secondary = AccentDark,
            background = Color.White, surface = Color.White, onSurface = Ink),
        content = content
    )
}

/** The bar at the top of every inner screen: back arrow and title. */
@Composable
fun TitleBar(title: String, onBack: (() -> Unit)?) {
    Column(Modifier.fillMaxWidth().background(Color.White).statusBarsPadding()) {
        Row(Modifier.fillMaxWidth().height(60.dp).padding(horizontal = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            if (onBack != null) {
                IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back", tint = Ink) }
            } else {
                Spacer(Modifier.width(16.dp))
            }
            Text(title, color = Ink, fontSize = 21.sp, fontWeight = FontWeight.Bold)
        }
        Box(Modifier.fillMaxWidth().height(1.dp).background(Line))
    }
}

@Composable
fun Loading() {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator(color = Accent) }
}

@Composable
fun Empty(text: String) {
    Text(text, color = Muted, textAlign = TextAlign.Center, modifier = Modifier.fillMaxWidth().padding(32.dp))
}

/** A small coloured label: leave type, status, day code. */
@Composable
fun Pill(text: String, bg: Color, fg: Color = Ink) {
    Text(text, color = fg, fontSize = 12.sp, fontWeight = FontWeight.Medium,
        modifier = Modifier.clip(RoundedCornerShape(50)).background(bg).padding(horizontal = 10.dp, vertical = 3.dp))
}

fun leaveColor(type: String): Color = if (type == "Adhoc") AdhocFill else InformedFill

fun statusPill(status: String): Pair<Color, Color> = when (status) {
    "approved" -> Color(0xFFDCFCE7) to Good
    "rejected" -> Color(0xFFFEE2E2) to Bad
    else -> Color(0xFFFEF3C7) to Color(0xFFB45309)
}

/** A ring showing how much of a total is left (leave balance). */
@Composable
fun Ring(value: Double, total: Double, size: Dp, stroke: Dp, color: Color, content: @Composable () -> Unit) {
    Box(Modifier.size(size), contentAlignment = Alignment.Center) {
        Canvas(Modifier.size(size)) {
            val s = stroke.toPx()
            val arc = Size(this.size.width - s, this.size.height - s)
            drawArc(Line, 0f, 360f, false, Offset(s / 2, s / 2), arc, style = Stroke(s))
            if (total > 0 && value > 0) {
                drawArc(color, -90f, (360f * (value / total).coerceAtMost(1.0)).toFloat(), false, Offset(s / 2, s / 2), arc,
                    style = Stroke(s, cap = StrokeCap.Round))
            }
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally) { content() }
    }
}

/** One row in the home screen's list of things to do. */
@Composable
fun ActionRow(title: String, subtitle: String, icon: androidx.compose.ui.graphics.vector.ImageVector, tint: Color, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(44.dp).clip(RoundedCornerShape(12.dp)).background(tint.copy(alpha = 0.12f)), contentAlignment = Alignment.Center) {
            Icon(icon, null, tint = tint, modifier = Modifier.size(24.dp))
        }
        Spacer(Modifier.width(14.dp))
        Column(Modifier.weight(1f)) {
            Text(title, color = Ink, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
            Text(subtitle, color = Muted, fontSize = 13.sp)
        }
        Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, null, tint = Muted)
    }
}

@Composable
fun KeyValue(k: String, v: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 6.dp), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(k, color = Muted)
        Spacer(Modifier.width(12.dp))
        Text(v, color = Ink, fontWeight = FontWeight.Medium, textAlign = TextAlign.End)
    }
}

/** 2026-10-05 -> 05/10/2026 */
fun dmy(iso: String): String = if (iso.length >= 10) "${iso.substring(8, 10)}/${iso.substring(5, 7)}/${iso.substring(0, 4)}" else iso

val MONTHS = listOf("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
    "November", "December")

fun rupees(v: Double): String = "₹ " + String.format(java.util.Locale.ENGLISH, "%,.0f", v)

fun num(v: Double): String = if (v % 1.0 == 0.0) v.toLong().toString() else v.toString()
